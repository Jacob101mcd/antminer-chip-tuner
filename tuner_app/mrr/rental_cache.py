"""In-memory rental status cache for MRR-configured rigs."""

from __future__ import annotations

import threading
import time

from tuner_app import state
from tuner_app.config.effective import canonical_miner_key
from tuner_app.mrr.client import MRRClient
from tuner_app.mrr.helpers import is_rig_rented


class RentalCache:
    """60-second TTL cache of MRR rental status, keyed by canonical MAC.

    A daemon thread refreshes entries every 60 seconds. API calls are spread
    across the window (~60/N seconds apart) to avoid hammering the MRR API
    when many rigs are configured.

    Public read/write methods (``get``, ``refresh_one``) accept either a MAC
    (v4 callers) or an IP (legacy / pre-A12 HTTP route handlers) and resolve
    internally via ``canonical_miner_key``. Cache entries themselves are
    keyed by MAC so DHCP IP changes don't fragment the cache.

    Lock discipline:
      - state.config_lock is acquired ONCE per cycle (brief config read),
        released BEFORE any MRR API I/O.
      - self._lock (plain threading.Lock) protects _cache reads/writes.
        Never held simultaneously with state.config_lock.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the cache daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="rental-cache", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the daemon thread to stop (non-blocking — daemon exits on next check)."""
        self._stop_event.set()

    def get(self, identifier: str) -> dict | None:
        """Return the cached RentalStatus dict for *identifier*, or None if absent.

        Accepts either a MAC (canonical) or an IP (transitional). IP→MAC
        translation goes through ``canonical_miner_key``.
        """
        mac = canonical_miner_key(identifier)
        with self._lock:
            return self._cache.get(mac)

    def get_all(self) -> dict:
        """Return a shallow copy of the entire cache dict (mac -> RentalStatus)."""
        with self._lock:
            return dict(self._cache)

    # ── internals ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Daemon thread main loop: refresh every 60 seconds."""
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            self._refresh_cycle()
            elapsed = time.monotonic() - cycle_start
            remaining = 60.0 - elapsed
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

    def _refresh_cycle(self) -> None:
        """One full refresh pass: read config, call MRR API per rig, update cache."""
        # ── 1. Read all config under ONE config_lock block; release before I/O ──
        with state.config_lock:
            fo = state.CONFIG["fleet_ops"]
            mrr_enabled = bool(fo.get("MRR_ENABLED", False))
            api_key = str(fo.get("MRR_API_KEY", "") or "")
            api_secret = str(fo.get("MRR_API_SECRET", "") or "")
            # MRR_RIG_ID is a per-platform tuning key, not fleet_ops.
            # Use a fleet-wide default of 0 (no rig) from the epic bucket.
            # TODO(phase-4): rental_cache reads MRR_RIG_ID from defaults["epic"] for fleet-wide
            # fallback. Once the per-platform UI lands and operators can diverge defaults per
            # platform, this should resolve from the matching miner's platform bucket
            # (mirror the pattern in tuner_app/http_server/handlers/miners_routes.py:163-168).
            global_rig_id = int(state.CONFIG["defaults"].get("epic", {}).get("MRR_RIG_ID") or 0)
            # Iterate MINER_CONFIGS directly — v4 fleet roster keyed by MAC.
            # Each entry's "ip" field is what the dashboard renders; the cache
            # key is the MAC so DHCP IP changes don't fragment cache state.
            miner_entries = [
                (mac, dict(ov)) for mac, ov in state.MINER_CONFIGS.items() if isinstance(ov, dict)
            ]
        # config_lock released here — no I/O inside the lock

        # ── 2. Build (mac, rig_id) pairs for any miner that is MRR-configured ──
        # A miner is MRR-configured when its per-miner MRR_RIG_ID > 0, or when
        # the global MRR_RIG_ID > 0 (a fleet-wide default rig). We populate
        # cache entries for ALL these miners even when MRR is globally disabled
        # / has no credentials, so the dashboard can surface *why* the rental
        # column is blank (e.g. "MRR off" vs. "credentials missing") rather
        # than silently rendering em-dash.
        pairs: list[tuple[str, int]] = []
        for mac, entry in miner_entries:
            rig_id = self._extract_rig_id(entry, global_rig_id)
            if rig_id > 0:
                pairs.append((mac, rig_id))

        # Drop entries for MACs no longer being tracked (miner removed or RIG_ID zeroed).
        pair_macs = {mac for mac, _ in pairs}
        with self._lock:
            for stale_mac in [mac for mac in self._cache if mac not in pair_macs]:
                del self._cache[stale_mac]

        if not pairs:
            return

        # ── 3. If MRR is not fully configured, write diagnostic entries ──────
        # (rather than leaving the cache empty, which would render as em-dash
        # in the UI and obscure why). Distinguish "disabled" from "no_creds"
        # so the operator knows which knob to flip.
        if not mrr_enabled or not api_key or not api_secret:
            if not mrr_enabled:
                state_value = "disabled"
                error_msg = "MRR is disabled — turn on MRR_ENABLED in fleet settings"
            else:
                state_value = "no_creds"
                error_msg = "MRR credentials not configured — set MRR_API_KEY and MRR_API_SECRET"
            now = time.time()
            with self._lock:
                for mac, rig_id in pairs:
                    self._cache[mac] = {
                        "rented": None,
                        "rig_id": rig_id,
                        "fetched_ts": now,
                        "error": error_msg,
                        "state": state_value,
                    }
            return

        # ── 4. Throttled API calls (spacing = 60/N, min 1 s) ─────────────────
        spacing = max(1.0, 60.0 / len(pairs))
        client = MRRClient(api_key, api_secret)

        for i, (mac, rig_id) in enumerate(pairs):
            if self._stop_event.is_set():
                break
            try:
                rig = client.get_rig(rig_id)
                rented = is_rig_rented(rig)
                entry: dict = {
                    "rented": rented,
                    "rig_id": rig_id,
                    "fetched_ts": time.time(),
                    "error": None,
                    "state": "live",
                }
            except Exception as exc:
                entry = {
                    "rented": None,
                    "rig_id": rig_id,
                    "fetched_ts": time.time(),
                    "error": str(exc),
                    "state": "error",
                }

            with self._lock:
                self._cache[mac] = entry

            # Sleep between calls (not before the first call)
            if i < len(pairs) - 1 and self._stop_event.wait(timeout=spacing):
                break  # daemon stopping mid-cycle

    @staticmethod
    def _extract_rig_id(entry: dict, global_rig_id: int) -> int:
        """Resolve effective MRR_RIG_ID for a miner entry.

        v4: cross-platform per-miner override (top level) wins → per-platform
        override (platforms[fw].MRR_RIG_ID, fw = current_firmware) → global
        fleet default.
        v3 fallback (no platforms key): flat MRR_RIG_ID at the entry's top level.
        """
        per_miner = entry.get("MRR_RIG_ID")
        if per_miner:
            return int(per_miner or 0)
        platforms = entry.get("platforms")
        if isinstance(platforms, dict):
            fw = entry.get("current_firmware") or "epic"
            fw_overrides = platforms.get(fw, {})
            if isinstance(fw_overrides, dict) and fw_overrides.get("MRR_RIG_ID"):
                return int(fw_overrides.get("MRR_RIG_ID") or 0)
        return int(global_rig_id or 0)

    # ── public refresh-on-demand hooks ───────────────────────────────────────
    def refresh_one(self, identifier: str, rig_id: int) -> None:
        """Refresh a single (identifier, rig_id) entry synchronously.

        *identifier* may be a MAC (canonical) or an IP (transitional). The
        cache is keyed by MAC; IP→MAC translation goes through
        ``canonical_miner_key``.

        Used after operator-driven config changes (rig assignment via the MRR
        pill popup, per-miner config tab) so the dashboard pill reflects the
        new state on the very next overview poll instead of waiting up to 60 s
        for the daemon's regular cycle.

        Bounded cost: at most one MRR API call (~1 s typical). Safe to call
        from the HTTP handler thread — short blocking is acceptable since the
        operator just clicked Save and is awaiting feedback.

        rig_id <= 0 means "rig was cleared" — drop the cache entry so the pill
        renders the "+ MRR" empty state on the next poll.
        """
        mac = canonical_miner_key(identifier)
        if rig_id <= 0:
            with self._lock:
                self._cache.pop(mac, None)
            return
        # Read fleet MRR config under a brief config_lock; release before I/O.
        with state.config_lock:
            fo = state.CONFIG["fleet_ops"]
            mrr_enabled = bool(fo.get("MRR_ENABLED", False))
            api_key = str(fo.get("MRR_API_KEY", "") or "")
            api_secret = str(fo.get("MRR_API_SECRET", "") or "")
        # If MRR isn't fully configured, write the same diagnostic shape the
        # daemon would write — keeps the pill informative ("MRR off" / "No
        # creds") instead of empty.
        if not mrr_enabled or not api_key or not api_secret:
            if not mrr_enabled:
                state_value = "disabled"
                error_msg = "MRR is disabled — turn on MRR_ENABLED in fleet settings"
            else:
                state_value = "no_creds"
                error_msg = "MRR credentials not configured — set MRR_API_KEY and MRR_API_SECRET"
            with self._lock:
                self._cache[mac] = {
                    "rented": None,
                    "rig_id": rig_id,
                    "fetched_ts": time.time(),
                    "error": error_msg,
                    "state": state_value,
                }
            return
        # Live API call — single-rig fetch, mirrors the daemon's per-rig path.
        try:
            rig = MRRClient(api_key, api_secret).get_rig(rig_id)
            entry: dict = {
                "rented": is_rig_rented(rig),
                "rig_id": rig_id,
                "fetched_ts": time.time(),
                "error": None,
                "state": "live",
            }
        except Exception as exc:
            entry = {
                "rented": None,
                "rig_id": rig_id,
                "fetched_ts": time.time(),
                "error": str(exc),
                "state": "error",
            }
        with self._lock:
            self._cache[mac] = entry


# Module-level singleton — imported by tuner_app.main and route handlers.
rental_cache = RentalCache()
