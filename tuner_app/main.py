"""Application bootstrap and command-line entry point.

Importing this module is deliberately side-effect free. Configuration loading,
data migrations, manager construction, and background threads begin only when
``bootstrap()`` or ``main()`` is called.
"""

from __future__ import annotations

import ipaddress
import os

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.config.persistence import load_config_from_disk
from tuner_app.constants import DATA_DIR, METRICS_DB_FILE
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import ThreadedHTTPServer, start_http_server
from tuner_app.logging_config import configure_logging
from tuner_app.manager.tuner_manager import TunerManager
from tuner_app.metrics.store import MetricsStore
from tuner_app.mrr.rental_cache import rental_cache
from tuner_app.profit.minerstat import MinerstatScheduler, load_minerstat_snapshot
from tuner_app.scanner.runner import Scanner

manager: TunerManager | None = None
scanner: Scanner | None = None
_bootstrapped = False


def _ensure_private_data_dir() -> None:
    """Create the runtime directory and tighten existing POSIX permissions."""
    # Runtime files include miner credentials and operational telemetry. Keep
    # every subsequently created file owner-only, regardless of the shell's
    # inherited umask.
    os.umask(0o077)
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
        for root, directories, filenames in os.walk(DATA_DIR):
            for directory in directories:
                os.chmod(os.path.join(root, directory), 0o700)
            for filename in filenames:
                os.chmod(os.path.join(root, filename), 0o600)
    except OSError:
        # Windows permissions are governed by the user's ACL rather than POSIX
        # mode bits. The directory still lives beneath that user's data root.
        pass


def bootstrap() -> tuple[TunerManager, Scanner]:
    """Initialize application state exactly once and return its singletons."""
    global manager, scanner, _bootstrapped
    if _bootstrapped:
        assert manager is not None and scanner is not None
        return manager, scanner

    configure_logging()
    _ensure_private_data_dir()
    apply_defaults()
    load_config_from_disk()
    manager = TunerManager(state.CONFIG)
    scanner = Scanner(manager)

    # Scanner routes use an injected singleton to avoid importing main from the
    # handler layer and creating a circular bootstrap dependency.
    import tuner_app.http_server.handlers.scanner_routes as scanner_routes

    scanner_routes.scanner = scanner
    _bootstrapped = True
    return manager, scanner


def _is_loopback_host(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


def build_server(
    host: str = "127.0.0.1",
    port: int = 8099,
    manager_instance: TunerManager | None = None,
) -> ThreadedHTTPServer:
    """Construct the HTTP server with fail-closed first-run binding.

    A non-loopback bind is permitted only after a dashboard password has been
    configured. Operators must opt in explicitly with ``TUNER_HOST``.
    """
    mgr = manager_instance
    if mgr is None:
        mgr, _ = bootstrap()
    if not _is_loopback_host(host) and not state.AUTH.get("password_hash"):
        raise RuntimeError(
            "Refusing a non-loopback bind before authentication is configured. "
            "Start on 127.0.0.1, complete setup, then set TUNER_HOST explicitly."
        )
    return start_http_server(host, port, TunerHandler, mgr)


def main() -> None:
    """Start the tuner and its background services until interrupted."""
    mgr, scan = bootstrap()
    load_minerstat_snapshot()
    state.metrics_store = MetricsStore(METRICS_DB_FILE)

    host = os.environ.get("TUNER_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("TUNER_PORT", "8099"))
    server = build_server(host, port, mgr)
    scheduler = MinerstatScheduler(mgr)

    scheduler.start()
    scan.start()
    rental_cache.start()
    state.metrics_store.start_retention_thread()
    display_host = "localhost" if _is_loopback_host(host) else host
    print(f"Antminer Chip Tuner running on http://{display_host}:{port}", flush=True)
    print(f"Configured miners: {len(state.CONFIG['fleet_ops']['MINER_IPS'])}", flush=True)
    poll_day = int(state.CONFIG["fleet_ops"].get("MINERSTAT_POLL_DAY", 0) or 0)
    if poll_day > 0:
        print(f"Minerstat auto-poll enabled on day {poll_day} of each month", flush=True)
    else:
        print("Minerstat auto-poll disabled (MINERSTAT_POLL_DAY=0)", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        scan.stop()
        rental_cache.stop()
        if state.metrics_store is not None:
            state.metrics_store.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
