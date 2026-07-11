"""Scanner daemon thread: discovers supported miners on configured IP ranges."""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from datetime import UTC, datetime

from tuner_app import state
from tuner_app.config.persistence import save_config_to_disk
from tuner_app.http_server.handlers.miners_routes import _register_miner_locked
from tuner_app.manager.bulk import _rekey_miner
from tuner_app.miner.braiins import BraiinsMinerAPI
from tuner_app.net.source_ip import resolve_source_ip
from tuner_app.scanner.discover import probe_miner
from tuner_app.scanner.ranges import parse_ip_ranges

logger = logging.getLogger(__name__)


class Scanner:
    """Background scanner that probes IP ranges for supported miners.

    One daemon thread runs a scan cycle on a configurable interval, waking
    early when request_scan_now() is called. Found miners are auto-registered
    when SCAN_AUTO_REGISTER is True, otherwise stashed in the status dict for
    review.

    Lock discipline:
      - state.config_lock is acquired ONCE per scan cycle (brief config read),
        released BEFORE any I/O or ThreadPoolExecutor activity.
      - _register_locked() acquires state.config_lock, calls
        _register_miner_locked() + save_config_to_disk() inside, releases,
        THEN calls manager.get_engine() outside the lock.
      - manager.get_engine() is NEVER called while holding state.config_lock.
    """

    def __init__(self, manager) -> None:
        self.manager = manager
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._status_lock = threading.Lock()
        self._last_status: dict = {
            "state": "idle",
            "last_run_started_at": None,
            "last_run_finished_at": None,
            "progress": 0,
            "total": 0,
            "discovered": [],
            "errors": [],
        }

    def start(self) -> None:
        """Start the scanner daemon thread (idempotent)."""
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True, name="scanner")
            self._thread.start()

    def stop(self) -> None:
        """Signal the scanner thread to stop (does not join — daemon)."""
        self._stop_event.set()
        self._wake_event.set()

    def request_scan_now(self) -> None:
        """Wake the scanner thread to run a scan cycle immediately."""
        self._wake_event.set()

    def get_status(self) -> dict:
        """Return a shallow copy of the current scanner status dict."""
        with self._status_lock:
            return dict(self._last_status)

    # ── internals ────────────────────────────────────────────────────────────

    def _update_status(self, **kwargs) -> None:
        with self._status_lock:
            self._last_status.update(kwargs)

    def _append_status_list(self, key: str, value) -> None:
        """Thread-safe append to a list field in the status dict."""
        with self._status_lock:
            self._last_status[key].append(value)

    def _run(self) -> None:
        """Main loop: scan → sleep → repeat until stop_event set."""
        while not self._stop_event.is_set():
            interval_min = float(state.CONFIG["fleet_ops"].get("SCAN_INTERVAL_MIN", 0) or 0)
            if interval_min <= 0:
                # Manual-only mode: do not perform even an initial scan until
                # request_scan_now() wakes the daemon.
                self._wake_event.wait()
                self._wake_event.clear()
                if self._stop_event.is_set():
                    break
            try:
                self._scan_cycle()
            except Exception as exc:
                logger.exception("scanner cycle crashed")
                self._update_status(
                    state="idle",
                    last_run_finished_at=datetime.now(UTC).isoformat(),
                    errors=[f"scan cycle error: {exc}"],
                )
            interval_min = float(state.CONFIG["fleet_ops"].get("SCAN_INTERVAL_MIN", 0) or 0)
            if interval_min > 0:
                self._wake_event.wait(timeout=interval_min * 60.0)
                self._wake_event.clear()

    def _scan_cycle(self) -> None:
        """Run one full scan cycle: read config, probe IPs, register found miners."""
        # ── 1. Mark scanning state ──────────────────────────────────────────
        now_iso = datetime.now(UTC).isoformat()
        self._update_status(
            state="scanning",
            last_run_started_at=now_iso,
            progress=0,
            total=0,
            discovered=[],
            errors=[],
        )

        # ── 2. Read all config values under ONE config_lock block ───────────
        with state.config_lock:
            fo = state.CONFIG["fleet_ops"]
            ranges_raw = list(fo.get("SCAN_IP_RANGES", []))
            blacklist_raw = list(fo.get("SCAN_IP_BLACKLIST", []))
            passwords = list(fo.get("SCAN_PASSWORDS", ["letmein"]))
            timeout = float(fo.get("SCAN_TIMEOUT_SEC", 2.0))
            concurrency = int(fo.get("SCAN_CONCURRENCY", 1024))
            auto_register = bool(fo.get("SCAN_AUTO_REGISTER", True))
            api_port = int(fo.get("API_PORT", 4028))
            source_ip = str(fo.get("SOURCE_IP", "") or "")
            known_ips = set(fo.get("MINER_IPS", []))
            existing_miners = list(fo.get("MINER_IPS", []))
        # config_lock released here — no I/O or executor activity inside it

        # ── 3. Parse ranges + blacklist ─────────────────────────────────────
        try:
            all_ips = parse_ip_ranges(ranges_raw)
        except ValueError as exc:
            self._update_status(
                state="idle",
                last_run_finished_at=datetime.now(UTC).isoformat(),
                errors=[f"SCAN_IP_RANGES: {exc}"],
            )
            return
        try:
            blacklist_ips = parse_ip_ranges(blacklist_raw)
        except ValueError as exc:
            # Fail closed: a malformed blacklist must NOT silently let us probe
            # IPs the operator told us to skip.
            self._update_status(
                state="idle",
                last_run_finished_at=datetime.now(UTC).isoformat(),
                errors=[f"SCAN_IP_BLACKLIST: {exc}"],
            )
            return
        blacklist_set = {str(ip) for ip in blacklist_ips}

        # ── 4. Filter out already-registered AND blacklisted IPs ────────────
        ips_to_scan = [
            str(ip) for ip in all_ips if str(ip) not in known_ips and str(ip) not in blacklist_set
        ]
        if not ips_to_scan:
            self._update_status(
                state="idle",
                last_run_finished_at=datetime.now(UTC).isoformat(),
            )
            return

        # ── 5. Resolve source_ip ONCE for the entire scan cycle ─────────────
        # Per-probe resolve_source_ip() takes 3s × (1 + N_interfaces) for unreachable
        # IPs (it probes every local interface), which dominates scan time for /16-class
        # ranges. We use the explicit CONFIG override if set, otherwise auto-detect
        # against ONE existing reachable miner, otherwise fall back to OS default.
        if source_ip:
            scan_source_ip = source_ip
        elif existing_miners:
            scan_source_ip = resolve_source_ip(existing_miners[0], api_port)
        else:
            scan_source_ip = ""

        # ── 6. Update total count ────────────────────────────────────────────
        self._update_status(total=len(ips_to_scan))

        # ── 7. Concurrent probe (no config_lock held) ───────────────────────
        discovered: list[dict] = []
        errors: list[str] = []
        futures: dict[concurrent.futures.Future, str] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            for ip_str in ips_to_scan:
                if self._stop_event.is_set():
                    break
                fut = executor.submit(
                    probe_miner,
                    ip_str,
                    source_ip=scan_source_ip,
                    api_port=api_port,
                    passwords=passwords,
                    timeout=timeout,
                )
                futures[fut] = ip_str

            # ── 8. Process results with incremental updates ──────────────────
            for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                result = future.result()  # probe_miner never raises
                if result.password_found is not None:
                    entry = {
                        "ip": result.ip,
                        "hostname": result.hostname,
                        "password": result.password_found,
                    }
                    discovered.append(entry)
                    self._append_status_list("discovered", entry)
                    if auto_register:
                        if result.firmware_type is None:
                            logger.warning(
                                "%s: skipping registration — probe returned unknown firmware type",
                                result.ip,
                            )
                        else:
                            try:
                                self._register_locked(
                                    result.ip,
                                    result.password_found,
                                    result.firmware_type,
                                    summary_raw=result.summary_raw,
                                    mac=result.mac,
                                    id_synthesized=result.id_synthesized,
                                )
                            except Exception as exc:
                                err = f"{result.ip}: register failed: {exc}"
                                errors.append(err)
                                self._append_status_list("errors", err)
                elif result.error:
                    err = f"{result.ip}: {result.error}"
                    errors.append(err)
                    self._append_status_list("errors", err)
                self._update_status(progress=completed)

        # ── 9. Update final status ───────────────────────────────────────────
        self._update_status(
            state="idle",
            last_run_finished_at=datetime.now(UTC).isoformat(),
            discovered=discovered,
            errors=errors,
        )

    def _register_locked(
        self,
        ip: str,
        password: str,
        firmware_type: str,
        summary_raw: dict | None = None,
        *,
        mac: str | None = None,
        id_synthesized: bool = False,
    ) -> None:
        """Register a discovered miner under config_lock, then spawn engine.

        firmware_type must be a non-None string — callers are responsible for
        guarding against None before calling this method.

        *mac* is the canonical MAC (or synth ID) returned by ``probe_miner``.
        Optional for backward compatibility with tests; when omitted, falls back
        to ``synthesize_mac_id(ip)`` so the v4 entry is still well-formed.

        Behavior A — Braiins registration-time MAC fetch:
          When firmware_type == "braiins" AND (mac is None OR id_synthesized),
          instantiate BraiinsMinerAPI and call summary(). If summary.mac is
          non-empty, replace mac with the real one and set id_synthesized=False.
          Any failure leaves probe values unchanged (logs a warning). The
          unauthenticated /api/v1/version/ probe response doesn't carry MAC,
          so registration time is the natural place to do the auth+fetch.

        Behavior B — Opportunistic synth-to-real re-key:
          When the final mac is non-synth (id_synthesized=False), scan
          MINER_CONFIGS for any OTHER entry where key != mac, entry ip == ip,
          and entry id_synthesized is True. For each match, call _rekey_miner
          BEFORE _register_miner_locked writes the new entry, so synth entry
          data (PASSWORD, platforms) gets migrated to the real-mac key. This
          gives existing synth-registered miners a self-healing upgrade path
          when the next scan finds a real MAC for the same IP.

        LOCK CONTRACT:
          - Acquires state.config_lock for the brief synth-key snapshot
            (reads MINER_CONFIGS), releases BEFORE calling _rekey_miner
            (which re-acquires it on its own — non-reentrant lock would
            deadlock if held).
          - Acquires state.config_lock again for the _register_miner_locked
            write + save_config_to_disk(), releases before manager.get_engine.
          - manager.get_engine() is NEVER called while holding state.config_lock.

        If `summary_raw` is provided (the JSON the probe already fetched), the
        new engine's `last_summary` is pre-populated so the fleet overview shows
        real hostname/model/state immediately, instead of em-dashes until the
        operator clicks the row and triggers /tuner/live. Currently supported for
        ePIC + Bixbit (LuxOS + Braiins probes return version-only data
        insufficient to construct a MinerSummary).
        """
        # ── Behavior A: Braiins registration-time MAC fetch ──────────────────
        if firmware_type == "braiins" and (mac is None or id_synthesized):
            with state.config_lock:
                api_port = int(state.CONFIG["fleet_ops"].get("API_PORT", 4028))
            try:
                api = BraiinsMinerAPI(ip, port=api_port, password=password)
                fetched = api.summary()
                if fetched.mac:
                    mac = fetched.mac
                    id_synthesized = False
                    logger.info("[%s] Braiins MAC discovered via API: %s", ip, mac)
            except Exception as ex:
                logger.warning("[%s] Braiins MAC fetch failed: %s", ip, ex)

        if mac is None:
            # Defensive fallback: callers above probe_miner always pass a MAC,
            # but tests may invoke _register_locked without one. Synthesize so
            # MINER_CONFIGS entry is still well-formed (v4 schema requires MAC key).
            from tuner_app.net.mac_resolve import synthesize_mac_id

            mac = synthesize_mac_id(ip)
            id_synthesized = True

        # ── Behavior B: Opportunistic synth-to-real re-key ───────────────────
        # Only when we have a confirmed real (non-synth) MAC do we migrate
        # any existing synth entries for this same IP. This runs BEFORE
        # _register_miner_locked so the synth entry's platforms/PASSWORD data
        # gets migrated to the real-mac key first. Lock contract: read
        # MINER_CONFIGS under a brief lock to snapshot keys, then RELEASE
        # the lock before calling _rekey_miner (which acquires it itself —
        # state.config_lock is non-reentrant, so it must be released first.
        if not id_synthesized:
            with state.config_lock:
                synth_keys = [
                    k
                    for k, v in state.MINER_CONFIGS.items()
                    if k != mac
                    and isinstance(v, dict)
                    and v.get("ip") == ip
                    and v.get("id_synthesized") is True
                ]
            for synth_key in synth_keys:
                try:
                    _rekey_miner(synth_key, mac, manager=self.manager)
                    logger.info(
                        "[%s] opportunistic synth-to-real re-key: %s -> %s",
                        ip,
                        synth_key,
                        mac,
                    )
                except ValueError as ex:
                    # Conflict (mac already registered) — log and continue.
                    logger.warning(
                        "[%s] synth-to-real re-key failed (%s -> %s): %s",
                        ip,
                        synth_key,
                        mac,
                        ex,
                    )
                except Exception as ex:
                    logger.warning(
                        "[%s] synth-to-real re-key error: %s",
                        ip,
                        ex,
                    )

        with state.config_lock:
            prior_entry = state.MINER_CONFIGS.get(mac)
            prior_ip = prior_entry.get("ip") if isinstance(prior_entry, dict) else None
            _register_miner_locked(
                mac=mac,
                ip=ip,
                password=password,
                firmware_type=firmware_type,
                id_synthesized=id_synthesized,
            )
            save_config_to_disk()
        # Lock released — safe to spawn engine thread / refresh now
        if prior_entry is not None and prior_ip and prior_ip != ip:
            # Known MAC, IP changed — retarget the live engine without teardown.
            try:
                self.manager.refresh_engine_ip(mac, ip)
            except Exception:
                logger.exception(
                    "scanner: failed to refresh engine IP for %s (%s -> %s)",
                    mac,
                    prior_ip,
                    ip,
                )
        engine = self.manager.get_engine(mac)
        if (
            summary_raw is not None
            and firmware_type in {"epic", "bixbit"}
            and engine.last_summary is None
            and (engine.thread is None or not engine.thread.is_alive())
        ):
            from tuner_app.miner.registry import SUMMARY_PARSER_REGISTRY

            parser = SUMMARY_PARSER_REGISTRY.get(firmware_type)
            if parser is not None:
                try:
                    engine.last_summary = parser(summary_raw)
                    engine.last_update = time.time()
                except Exception:
                    logger.exception("scanner: failed to pre-populate last_summary for %s", ip)
