from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time

from tuner_app.miner.base import MinerAPI
from tuner_app.miner.exceptions import (
    MinerCommandError,
    MinerOfflineError,
    UnsafeVoltageBoundsError,
)
from tuner_app.miner.types import BoardSummary, HardwareTopology, MinerSummary

logger = logging.getLogger(__name__)
LUXOS_DEFAULT_PORT = 4028
RECV_HARD_CAP_BYTES = 1024 * 1024
RECV_CHUNK_SIZE = 4096
DEFAULT_TIMEOUT_SEC = 15


class _LuxosTransport:
    def __init__(
        self,
        ip: str,
        port: int = LUXOS_DEFAULT_PORT,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        min_conn_interval_sec: float = 1.0,
        offline_backoff_sec: float = 30.0,
    ):
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self._min_conn_interval_sec = min_conn_interval_sec
        self._offline_backoff_sec = offline_backoff_sec
        self._last_conn_attempt_monotonic: float | None = None
        self._offline_until_monotonic: float | None = None
        # RLock (reentrant) so _apply_rate_limit() can acquire the lock even
        # when called from inside _send_raw() while a session-locked caller
        # (send_cmd with requires_session=True) already holds it. Issue #26.
        self._lock = threading.RLock()
        self._session_id: str | None = None

    def _apply_rate_limit(self) -> None:
        """Enforce a per-instance min interval between TCP connection attempts.

        Uses two locked critical sections separated by an unlocked sleep so
        concurrent callers do not pile up behind the lock during long backoff
        sleeps (up to 300 s for offline backoff). Lock 1 reads rate-state and
        computes the target wake deadline; sleep happens UNLOCKED; Lock 2
        updates _last_conn_attempt_monotonic after waking.

        Uses time.monotonic (not time.time) so NTP step events cannot backflow
        the gate. First call (no prior timestamp) does not sleep.

        Also enforces the offline backoff window: if a prior ConnectionRefusedError
        set _offline_until_monotonic, sleep until the later of (regular spacing
        wake, offline_until). Whichever deadline is further in the future wins.
        """
        # Lock 1: read state and compute sleep duration, then release.
        with self._lock:
            now = time.monotonic()

            # Compute the regular-spacing wake time (None if first call)
            if self._last_conn_attempt_monotonic is None:
                regular_wake: float | None = None
            else:
                regular_wake = self._last_conn_attempt_monotonic + self._min_conn_interval_sec

            # Compute the offline-backoff wake time (None if not set or already past)
            offline_wake: float | None = self._offline_until_monotonic

            # Determine which deadline is later
            candidates = [t for t in (regular_wake, offline_wake) if t is not None]
            if candidates:
                target = max(candidates)
                sleep_for = max(0.0, target - now)
            else:
                sleep_for = 0.0

        # Unlocked sleep: concurrent callers compute and sleep in parallel.
        if sleep_for > 0.0:
            time.sleep(sleep_for)

        # Lock 2: update the last-attempt timestamp after waking.
        with self._lock:
            self._last_conn_attempt_monotonic = time.monotonic()

    def send_cmd(self, cmd: str, *params, requires_session: bool = False) -> dict:
        # If no session needed: call _send_raw directly, no lock.
        # Params MUST be serialized into the payload — many LuxOS read cmds
        # (voltageget, frequencyget, healthchipget) require a board_id /
        # chip_id parameter. A paramless `voltageget` in particular silently
        # blocks the LuxOS API server for ~10-15 s on LUXminer 2026.4.3,
        # poisoning every subsequent connection in Phase 0 with
        # ConnectionRefusedError. Format mirrors the session-required path
        # (`,`-joined parameter string).
        if not requires_session:
            payload: dict = {"command": cmd}
            if params:
                payload["parameter"] = ",".join(str(p) for p in params)
            return self._send_raw(payload)
        # Session needed: lock the entire logon+mutate+logoff cycle
        with self._lock:
            if self._session_id is None:
                self._session_id = self._open_session()
            session_id = self._session_id
            try:
                result = self._execute_with_session_refresh(cmd, session_id, params)
                return result
            finally:
                self._logoff_locked(session_id)
                self._session_id = None

    def close_session(self) -> None:
        # Idempotent logoff
        with self._lock:
            if self._session_id is None:
                return
            session_id = self._session_id
            self._session_id = None
            self._logoff_locked(session_id)

    def _execute_with_session_refresh(self, cmd: str, session_id: str, params: tuple) -> dict:
        # Build payload with session_id prepended to params
        current_session = session_id
        for attempt in range(2):
            payload = {
                "command": cmd,
                "parameter": ",".join([current_session] + list(params)),
            }
            try:
                return self._send_raw(payload)
            except MinerCommandError as exc:
                if attempt == 0 and "session" in str(exc).lower():
                    logger.info("luxos session expired for %s, refreshing: %s", self.ip, exc)
                    self._session_id = self._open_session()
                    current_session = self._session_id
                    continue
                raise
        # unreachable — loop either returns or raises
        raise MinerCommandError(f"{cmd}: session refresh exhausted")

    def _send_raw(self, payload: dict) -> dict:
        # Issue #26: throttle TCP connection attempts so Phase 0's burst (3 cmds
        # back-to-back, retried 3x) cannot trip LuxOS port 4028 into refusing
        # all connections. Default 1.0s spacing — operator-tunable via the
        # LUXOS_MIN_CONN_INTERVAL_SEC config knob.
        self._apply_rate_limit()
        cmd = payload.get("command", "unknown")
        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout) as sock:
                sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                response = b""
                while True:
                    chunk = sock.recv(RECV_CHUNK_SIZE)
                    if not chunk:
                        break
                    if len(response) + len(chunk) > RECV_HARD_CAP_BYTES:
                        raise MinerCommandError(
                            f"{cmd}: response exceeded {RECV_HARD_CAP_BYTES} byte cap"
                        )
                    response += chunk
                    try:
                        result = json.loads(response.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if result["STATUS"][0]["STATUS"] == "E":
                        msg = result["STATUS"][0]["Msg"]
                        desc = result["STATUS"][0].get("Description", "")
                        raise MinerCommandError(f"{cmd}: {msg} ({desc})".rstrip(" ()"))
                    return result
                raise MinerCommandError(f"{cmd}: incomplete response ({len(response)} bytes)")
        except (
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionError,
            socket.gaierror,
            TimeoutError,
            OSError,
        ) as exc:
            # Issue #33: ConnectionRefusedError means the port is actively refusing
            # connections (miner overloaded / port storm). Set a backoff window so
            # _apply_rate_limit sleeps past the offline deadline on the next call.
            # Other transient errors (timeout, reset, OS, gaierror) do NOT set the
            # window — they indicate general offline / unreachable, not a port storm.
            if isinstance(exc, ConnectionRefusedError) and self._offline_backoff_sec > 0:
                with self._lock:
                    self._offline_until_monotonic = time.monotonic() + self._offline_backoff_sec
            raise MinerOfflineError(f"{cmd}: {exc}") from exc

    def _open_session(self) -> str:
        result = self._send_raw({"command": "logon"})
        session_id = result["SESSION"][0]["SessionID"]
        self._session_id = session_id
        return session_id

    def _logoff_locked(self, session_id: str) -> None:
        try:
            self._send_raw({"command": "logoff", "parameter": session_id})
        except Exception as exc:
            logger.warning("luxos logoff failed for %s: %s", self.ip, exc)


class LuxosMinerAPI(MinerAPI):
    """LuxOS miner API client.

    All wire calls are delegated to the private _LuxosTransport instance
    (self._transport), which handles TCP connection lifecycle, recv-loop with
    1 MB hard cap, and session logon/logoff/refresh for mutating commands.

    Three per-instance caches protect expensive multi-call reads:
    - _limits_cache: LIMITS dict from ``limits`` cmd (static per instance).
    - _capabilities_cache: ePIC-shaped capabilities dict (static per instance).
    - _chip_health_cache: (monotonic_ts, list[BoardSummary]) for healthchipget
      data shared between temps_chip() and hashrate() to avoid double-firing
      324 TCP calls per monitor cycle.

    All cache reads/writes are protected by self._cache_lock.
    """

    CHIP_HEALTH_CACHE_TTL_SEC = 5.0

    def __init__(
        self,
        ip: str,
        port: int = LUXOS_DEFAULT_PORT,
        password: str = "letmein",
        min_conn_interval_sec: float = 1.0,
        offline_backoff_sec: float = 30.0,
    ):
        super().__init__(ip, port, password)
        self._transport = _LuxosTransport(
            ip,
            port,
            min_conn_interval_sec=min_conn_interval_sec,
            offline_backoff_sec=offline_backoff_sec,
        )
        self._capabilities_cache: dict | None = None
        self._limits_cache: dict | None = None
        self._model_cache: str | None = None
        # (monotonic_time, list[BoardSummary]) — shared by temps_chip + hashrate
        self._chip_health_cache: tuple[float, list] | None = None
        # Tracks the last successfully-applied set_perpetualtune state so
        # repeat calls with the same value (every Phase 0 entry) skip the
        # 6 TCP cmds (atmset + autotunerset, each session-locked = 3 TCP).
        # None = unknown / never set / invalidated by error. Lives only for
        # this MinerAPI instance — engine recreation resets it naturally.
        self._perpetualtune_cached: bool | None = None
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Trivial capability / identity methods
    # ------------------------------------------------------------------

    def firmware_type(self) -> str:
        return "luxos"

    def tuning_strategy(self) -> str:
        return "voltage_chip_tune"

    def authenticate(self) -> bool:
        try:
            self._transport.send_cmd("version", requires_session=False)
            return True
        except Exception:
            return False

    def supports_per_chip_tuning(self) -> bool:
        return True

    def has_external_power_limit(self) -> bool:
        return True

    def has_capabilities_endpoint(self) -> bool:
        return True

    def has_internal_perpetual_tune(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Stubs — implemented in subsequent units
    # ------------------------------------------------------------------

    def summary(self) -> MinerSummary:
        """Fetch and synthesize a MinerSummary from the LuxOS miner.

        Calls (in order):
          1. ``summary`` (REQUIRED — propagates errors; the engine's normal
             retry path triggers if this fails).
          2. ``version`` (defensive — model degrades to None on failure).
          3. ``stats`` (defensive — legacy power_w fallback).
          4. ``tunerstatus`` (defensive — preferred power_w source on
             LUXminer 2026.4.3+; degrades to STATS fallback on failure).
          5. ``fans`` (defensive — fan_speed degrades to 0 on failure).
          6. ``config`` (defensive — hostname degrades to None on failure).
          7. ``power`` (defensive — input power_w from POWER[0]['Watts']).
          8. ``voltageget`` (defensive — output_voltage_mv from boards 0/1/2;
             first board with Voltage>0 used; all-zero → None).

        Aux command failures are swallowed so a transient miner-side stat
        timeout does not break the entire status response. Each aux call is
        wrapped independently so one failure does not cascade.
        """
        raw_summary = self._transport.send_cmd("summary")
        raw_version: dict | None = None
        try:  # noqa: SIM105
            raw_version = self._transport.send_cmd("version")
        except Exception:
            pass
        raw_stats: dict | None = None
        try:  # noqa: SIM105
            raw_stats = self._transport.send_cmd("stats")
        except Exception:
            pass
        raw_tunerstatus: dict | None = None
        try:  # noqa: SIM105
            raw_tunerstatus = self._transport.send_cmd("tunerstatus")
        except Exception:
            pass
        raw_fans: dict | None = None
        try:  # noqa: SIM105
            raw_fans = self._transport.send_cmd("fans")
        except Exception:
            pass
        raw_config: dict | None = None
        try:  # noqa: SIM105
            raw_config = self._transport.send_cmd("config")
        except Exception:
            pass
        raw_power: dict | None = None
        try:  # noqa: SIM105
            raw_power = self._transport.send_cmd("power")
        except Exception:
            pass
        result = MinerSummary.from_luxos(
            raw_summary,
            raw_version=raw_version,
            raw_stats=raw_stats,
            raw_tunerstatus=raw_tunerstatus,
            raw_fans=raw_fans,
            raw_config=raw_config,
            raw_power=raw_power,
        )
        for board_idx in (0, 1, 2):
            try:
                voltage_raw = self._transport.send_cmd("voltageget", str(board_idx))
                v = float(voltage_raw["VOLTAGE"][0]["Voltage"])
                if v > 0:
                    result.output_voltage_mv = v * 1000
                    break
            except Exception:
                continue
        return result

    def summary_lite(self) -> MinerSummary:
        """Single-cmd liveness probe — fires only ``summary`` (1 TCP).

        Populates ``operating_state`` and ``hashrate_ths`` from the LuxOS
        ``summary`` response; all other fields default to None / 0. Used
        by recovery polling and Phase 0's post-perpetualtune mining-state
        recheck so a 10s poll loop doesn't fire 10 TCP cmds per cycle and
        trip port 4028 into refusing connections.
        """
        raw_summary = self._transport.send_cmd("summary")
        return MinerSummary.from_luxos(raw_summary)

    def clocks(self) -> list[BoardSummary]:
        boards = []
        for board_idx in range(self.hardware_topology().num_boards):
            raw = self._transport.send_cmd("frequencyget", str(board_idx))
            freqs = [float(f) for f in raw["FREQS"][0]["Freqs"]]
            boards.append(
                BoardSummary(
                    index=board_idx,
                    hashrate_ths=0.0,
                    freq_mhz=0.0,
                    chip_freqs_mhz=freqs,
                )
            )
        return boards

    def temps(self) -> list[BoardSummary]:
        """Return per-board inlet/outlet temperatures from LuxOS ``temps``.

        On LUXminer 2026.4.3 each ``TEMPS[i]`` entry has shape (verified live):
          ``{ID, TEMP, TopLeft, TopRight, BottomLeft, BottomRight}``
        The ``METADATA`` section labels these positions: ``Right`` columns
        are "Board Intake" (cold inlet air); ``Left`` columns are "Board
        Exhaust" (hot air after chips). Pre-fix, this method (1) read
        ``board_data["Board"]`` — does not exist, the index field is
        ``ID`` — and (2) labelled ``TopLeft`` (exhaust ~49°C) as
        ``temp_inlet_c`` and ``BottomRight`` (intake ~37°C) as
        ``temp_outlet_c``, swapping the semantics. Fixed via the metadata-
        accurate mapping plus defensive ``.get()`` reads.

        Inlet/outlet take the max across top/bottom positions so a single
        warm reading isn't masked by a cooler one — thermal protection
        should err high.
        """
        raw = self._transport.send_cmd("temps")
        boards = []
        for i, board_data in enumerate(raw.get("TEMPS", []) or []):
            if not isinstance(board_data, dict):
                continue
            tr = float(board_data.get("TopRight", 0) or 0)
            br = float(board_data.get("BottomRight", 0) or 0)
            tl = float(board_data.get("TopLeft", 0) or 0)
            bl = float(board_data.get("BottomLeft", 0) or 0)
            boards.append(
                BoardSummary(
                    index=int(board_data.get("ID", i) or i),
                    hashrate_ths=0.0,
                    freq_mhz=0.0,
                    temp_inlet_c=max(tr, br),
                    temp_outlet_c=max(tl, bl),
                )
            )
        return boards

    def temps_chip(self) -> list[BoardSummary]:
        """Return cached chip health data with per-chip temperatures populated."""
        return self._fetch_chip_health()

    def hashrate(self) -> list[BoardSummary]:
        """Return cached chip health data with health_pct and hashrate_per_chip_mhs populated."""
        return self._fetch_chip_health()

    def voltages(self) -> dict:
        """Return the raw ``voltageget`` response dict verbatim."""
        return self._transport.send_cmd("voltageget", "0")

    def capabilities(self) -> dict:
        """Return an ePIC-shaped capabilities dict, cached for the instance lifetime.

        Reads ``limits``, ``frequencyget,0``, and ``devdetails`` from the miner.
        The returned shape mirrors the keys the engine reads from ePIC's /capabilities:
          - Psu Info.Min Vout / Max Vout  (in mV, converted from LuxOS volts)
          - Performance Estimator.Chip Count  (chips per board)
          - Max HBs  (number of hashboards)
        """
        with self._cache_lock:
            if self._capabilities_cache is not None:
                return self._capabilities_cache

        limits_raw = self._transport.send_cmd("limits")
        limits_data = limits_raw["LIMITS"][0]
        devdetails_raw = self._transport.send_cmd("devdetails")
        num_boards = len(devdetails_raw["DEVDETAILS"])
        freqs_raw = self._transport.send_cmd("frequencyget", "0")
        chip_count = freqs_raw["FREQS"][0]["Count"]

        caps = {
            "Psu Info": {
                "Min Vout": limits_data["VoltageMin"] * 1000,
                "Max Vout": limits_data["VoltageMax"] * 1000,
            },
            "Performance Estimator": {
                "Chip Count": chip_count,
            },
            "Max HBs": num_boards,
        }

        with self._cache_lock:
            self._capabilities_cache = caps
        return caps

    def _fetch_chip_health(self) -> list[BoardSummary]:
        """Fetch per-chip health data via bulk per-board ``healthchipget``.

        ``healthchipget(board_id)`` (single positional param) returns ALL chips
        on that board in one CHIPS array — verified live on LUXminer 2026.4.3
        (board 0 → 108 entries, Chip 0 through Chip 107). One TCP per board
        instead of one per chip; with the 1.0 s rate gate this is the
        difference between ~3 seconds and ~5.4 minutes per call.

        Cached for 5 s (shared between ``temps_chip()`` and ``hashrate()``).

        Per-chip fields read with defensive ``.get(default)`` so a single
        malformed entry can't kill the whole batch:
          - ``GHS 5m`` (chip hashrate in GH/s) → ``hashrate_per_chip_mhs``
            (× 1000 for MH/s, matching ePIC's units) and feeds ``health_pct``
            (engine compares deltas to baseline; absolute units matter less
            than stability — ``DEAD_CHIP_SCORE`` default 1.0 trivially clears
            for any chip with non-trivial hashrate).
          - ``Healthy`` (``"Yes"`` / ``"No"`` / ``"Unknown"``) — a ``"No"``
            short-circuits ``health_pct`` to 0 so the dead-chip parker
            excludes it.
          - ``ChipTemp`` (per-chip temperature in C) → ``chip_temps_c``.
        """
        with self._cache_lock:
            if self._chip_health_cache is not None:
                ts, cached_boards = self._chip_health_cache
                if time.monotonic() - ts < self.CHIP_HEALTH_CACHE_TTL_SEC:
                    return cached_boards

        boards: list[BoardSummary] = []
        for board_idx in range(self.hardware_topology().num_boards):
            raw = self._transport.send_cmd("healthchipget", str(board_idx))
            chips = raw.get("CHIPS", []) or []

            health_pct: list[float] = []
            hashrate_per_chip: list[float] = []
            chip_temps: list[float] = []
            for chip_info in chips:
                if not isinstance(chip_info, dict):
                    continue
                ghs_5m = float(chip_info.get("GHS 5m", 0.0) or 0.0)
                healthy = str(chip_info.get("Healthy", "")).strip()
                chip_mhs = ghs_5m * 1000.0  # GH/s → MH/s
                score = 0.0 if healthy == "No" else chip_mhs

                health_pct.append(score)
                hashrate_per_chip.append(chip_mhs)
                chip_temps.append(float(chip_info.get("ChipTemp", 0.0) or 0.0))

            boards.append(
                BoardSummary(
                    index=board_idx,
                    hashrate_ths=0.0,
                    freq_mhz=0.0,
                    health_pct=health_pct,
                    hashrate_per_chip_mhs=hashrate_per_chip,
                    chip_temps_c=chip_temps,
                )
            )

        with self._cache_lock:
            self._chip_health_cache = (time.monotonic(), boards)
        return boards

    # ------------------------------------------------------------------
    # Voltage helpers and set_voltage
    # ------------------------------------------------------------------

    def _get_limits_cached(self) -> dict:
        """Return the cached LIMITS dict; fetches from miner on first call."""
        with self._cache_lock:
            if self._limits_cache is not None:
                return self._limits_cache
        raw = self._transport.send_cmd("limits")
        limits = raw["LIMITS"][0]
        with self._cache_lock:
            self._limits_cache = limits
        return limits

    def _snap_voltage_v(self, volts: float, step: float) -> float:
        """Round volts to the nearest LuxOS VoltageStepMin grid."""
        return round(round(volts / step) * step, 4)

    def _validate_voltage_v(self, volts: float, limits: dict) -> None:
        """Raise MinerCommandError if volts is outside [VoltageMin, VoltageMax]."""
        if not (limits["VoltageMin"] <= volts <= limits["VoltageMax"]):
            raise MinerCommandError(
                f"voltage {int(volts * 1000)} mV out of LuxOS range "
                f"[{int(limits['VoltageMin'] * 1000)}, {int(limits['VoltageMax'] * 1000)}] mV"
            )

    def set_voltage(self, mv: float) -> bool:
        """Set system-wide voltage.  Engine passes mV; LuxOS wire uses volts.

        Conversion boundary: mv / 1000.0 happens here; no mV/V confusion
        escapes this method. Voltage is snapped to the LuxOS step grid before
        sending to avoid firmware rejection.
        """
        limits = self._get_limits_cached()
        raw_min_v = limits.get("VoltageMin")
        raw_max_v = limits.get("VoltageMax")
        if not (
            isinstance(raw_min_v, (int, float))
            and not isinstance(raw_min_v, bool)
            and isinstance(raw_max_v, (int, float))
            and not isinstance(raw_max_v, bool)
            and math.isfinite(float(raw_min_v))
            and math.isfinite(float(raw_max_v))
            and raw_min_v >= 1.0
            and raw_max_v > raw_min_v
        ):
            raise UnsafeVoltageBoundsError(
                "refusing voltage mutation: LuxOS did not report valid live PSU bounds"
            )
        volts = mv / 1000.0
        volts = self._snap_voltage_v(volts, limits["VoltageStepMin"])
        self._validate_voltage_v(volts, limits)
        self._transport.send_cmd("voltageset", "0", str(volts), "0.05", requires_session=True)
        return True

    # ------------------------------------------------------------------
    # Clock setters
    # ------------------------------------------------------------------

    def set_clock_all(self, mhz: float) -> bool:
        """Set all chips on all boards to the same frequency (board-uniform form)."""
        for board_idx in range(self.hardware_topology().num_boards):
            self._transport.send_cmd(
                "frequencyset", str(board_idx), str(int(mhz)), requires_session=True
            )
        return True

    def set_clock_board(self, board_clocks: list) -> bool:
        """Set per-board uniform frequency.  board_clocks is [(board_idx, mhz), ...]."""
        for board_idx, mhz in board_clocks:
            self._transport.send_cmd(
                "frequencyset", str(board_idx), str(int(mhz)), requires_session=True
            )
        return True

    def set_clock_chip(self, board_index: int, chip_freqs: list) -> bool:
        """Set per-chip frequencies on one board with diff-and-skip + coalesce-uniform.

        chip_freqs is [(chip_idx, target_mhz), ...].

        Algorithm:
        1. Fetch current chip freqs via frequencyget (read-only, no session).
        2. Compute diff: only chips whose int(target) != int(current).
        3. If diff is empty: return True immediately (nothing to send).
        4. If all chips in chip_freqs share the same target freq AND ALL of them
           differ from current: send one board-uniform frequencyset (1 round-trip).
        5. Otherwise: send per-chip frequencyset for each differing chip
           (N round-trips for N differing chips — documented cost).
        """
        raw = self._transport.send_cmd("frequencyget", str(board_index))
        current_freqs = raw["FREQS"][0]["Freqs"]

        diff = []
        for chip_idx, target_mhz in chip_freqs:
            current = int(current_freqs[chip_idx]) if chip_idx < len(current_freqs) else 0
            if int(target_mhz) != current:
                diff.append((chip_idx, target_mhz))

        if not diff:
            return True

        # Coalesce: same target, full-board coverage, all differ → one board-wide call
        unique_targets = {int(mhz) for _, mhz in chip_freqs}
        if (
            len(unique_targets) == 1
            and len(chip_freqs) == len(current_freqs)
            and len(diff) == len(chip_freqs)
        ):
            self._transport.send_cmd(
                "frequencyset",
                str(board_index),
                str(int(chip_freqs[0][1])),
                requires_session=True,
            )
            return True

        for chip_idx, target_mhz in diff:
            self._transport.send_cmd(
                "frequencyset",
                str(board_index),
                str(int(target_mhz)),
                str(chip_idx),
                requires_session=True,
            )
        return True

    # ------------------------------------------------------------------
    # Perpetual tune + coin/pool management
    # ------------------------------------------------------------------

    def set_perpetualtune(self, enabled: bool) -> bool:
        """Enable or disable perpetual tuning.

        LuxOS has TWO SEPARATE firmware features that must both be toggled:
        - ``atmset`` (Adaptive Thermal Management)
        - ``autotunerset`` (frequency autotuner)

        Both calls must succeed; if either raises MinerCommandError, it
        propagates up to the engine unchanged. The instance-level cache
        skips both calls when ``enabled`` matches the last successfully
        applied value — this prevents Phase 0's idempotent
        ``set_perpetualtune(False)`` from re-firing 6 TCP cmds (2 cmds ×
        3 TCP per session cycle) on every retry. Cache is invalidated on
        any MinerCommandError so a partial-state failure forces the next
        call to re-issue both commands.
        """
        if self._perpetualtune_cached is not None and self._perpetualtune_cached == enabled:
            return True
        enable_str = "enabled=true" if enabled else "enabled=false"
        try:
            self._transport.send_cmd("atmset", enable_str, requires_session=True)
            self._transport.send_cmd("autotunerset", enable_str, requires_session=True)
        except MinerCommandError:
            self._perpetualtune_cached = None
            raise
        self._perpetualtune_cached = enabled
        return True

    def set_coin(self, coin: str, stratum_configs: list, unique_id: bool = False) -> bool:
        """Configure stratum pools idempotently and activate the first one.

        LuxOS ``addpool`` errors on a duplicate URL, so this method queries
        the current pool list first and only adds missing pools. This makes
        repeated calls (e.g., from MRR Phase 0 pool-push) safe.

        Steady-state fast path: if every stratum URL is already in the
        miner's pool list AND the first stratum URL is currently the
        Active pool, return after a single ``pools`` read with no
        session-required cmds. This prevents Phase 0 from firing 4-5 TCP
        cmds against LuxOS every time the engine restarts, which on
        2026.4.3 contributes to port 4028 connection-refusal storms.

        ``coin`` and ``unique_id`` are ignored — LuxOS firmware is SHA-256-only.
        """
        if not stratum_configs:
            return True

        pools_raw = self._transport.send_cmd("pools")
        pools_list = pools_raw.get("POOLS", [])
        existing_urls = {p["URL"] for p in pools_list}
        configured_urls = [c["pool"] for c in stratum_configs]
        first_url = configured_urls[0]

        # Fast path: all stratums present AND the first is already Active.
        all_present = all(u in existing_urls for u in configured_urls)
        first_pool = next((p for p in pools_list if p.get("URL") == first_url), None)
        first_active = first_pool is not None and first_pool.get("Status") == "Active"
        if all_present and first_active:
            return True

        # Add missing pools.
        for config in stratum_configs:
            url = config["pool"]
            user = config["login"]
            pass_val = config.get("password", "")
            if url not in existing_urls:
                self._transport.send_cmd("addpool", url, user, pass_val, requires_session=True)

        # Resolve the first pool's ID using the snapshot we already have when
        # nothing changed; otherwise re-read once after addpool to pick up the
        # newly-added pool ids.
        if all_present:
            target_pool = first_pool
        else:
            pools_raw = self._transport.send_cmd("pools")
            target_pool = next(
                (p for p in pools_raw.get("POOLS", []) if p.get("URL") == first_url),
                None,
            )

        if target_pool is None:
            return True
        pool_id = target_pool.get("POOL")
        if pool_id is None:
            return True

        # Switch to the first pool. If the call fails (stale active-pool
        # reading or transient firmware error), re-read pools once and try
        # again so a one-off transient doesn't get masked as success on the
        # next idempotent invocation.
        try:
            self._transport.send_cmd("switchpool", str(pool_id), requires_session=True)
        except MinerCommandError:
            refreshed = self._transport.send_cmd("pools")
            retry_pool = next(
                (p for p in refreshed.get("POOLS", []) if p.get("URL") == first_url),
                None,
            )
            if retry_pool is None or retry_pool.get("POOL") is None:
                raise
            self._transport.send_cmd("switchpool", str(retry_pool["POOL"]), requires_session=True)

        return True

    # ------------------------------------------------------------------
    # Mining state + power limit
    # ------------------------------------------------------------------

    def start_mining(self) -> bool:
        """Wake the miner from curtailed state (LuxOS curtail wakeup)."""
        self._transport.send_cmd("curtail", "wakeup", requires_session=True)
        return True

    def stop_mining(self) -> bool:
        """Put the miner into curtailed sleep state (LuxOS curtail sleep)."""
        self._transport.send_cmd("curtail", "sleep", requires_session=True)
        return True

    def reboot(self, delay: int = 0) -> bool:
        """Reboot the miner via LuxOS ``rebootdevice``.

        ``delay`` is ignored — LuxOS rebootdevice does not accept a delay arg.

        Wedge-handling: if the transport raises MinerCommandError (e.g.,
        repeated session failures on a wedged miner), it is re-raised as
        MinerOfflineError so the engine's retry escalation path triggers.
        """
        try:
            self._transport.send_cmd("rebootdevice", requires_session=True)
        except MinerCommandError as exc:
            raise MinerOfflineError(f"rebootdevice: {exc}") from exc
        return True

    def set_power_limit(self, watts: float) -> bool:
        """Set external power cap via LuxOS ``powertargetset``.

        LuxOS ``powertargetset`` requires a ``power=<watts>`` key=value payload
        — a bare positional ``<watts>`` returns "Invalid key/value format" on
        LUXminer 2026.4.3. This key/value form was confirmed on a supervised
        test unit.
        """
        self._transport.send_cmd("powertargetset", f"power={int(watts)}", requires_session=True)
        return True

    # ------------------------------------------------------------------
    # Hardware topology
    # ------------------------------------------------------------------

    def hardware_topology(self) -> HardwareTopology:
        """Return the miner's hardware topology, cached for the instance lifetime.

        Reads ``limits`` (PSU voltage range), ``frequencyget,0`` (chips per
        board), and ``devdetails`` (board count). If the LIMITS values are
        nonsensical (< 1 V or min >= max), falls back to the S21 spec range
        [11877, 15182] mV. If devdetails returns 0 boards, falls back to 3.

        For Antminer S21 hardware specifically, an additional model-aware
        clamp pulls the LuxOS-reported PSU range back to the manufacturer
        spec [11877, 15182] mV. LUXminer 2026.4.3 reports VoltageMax=15.48 V
        (15480 mV), 298 mV above the S21 PSU Type 193 spec — Phase V
        exploration would otherwise push voltage above hardware limits.
        Other models fall through to the unclamped LuxOS range.
        """
        with self._cache_lock:
            cached = getattr(self, "_topology_cache", None)
            if cached is not None:
                return cached

        limits = self._get_limits_cached()
        raw_min_v = limits.get("VoltageMin")
        raw_max_v = limits.get("VoltageMax")
        psu_bounds_verified = (
            isinstance(raw_min_v, (int, float))
            and not isinstance(raw_min_v, bool)
            and isinstance(raw_max_v, (int, float))
            and not isinstance(raw_max_v, bool)
            and math.isfinite(float(raw_min_v))
            and math.isfinite(float(raw_max_v))
            and raw_min_v >= 1.0
            and raw_max_v > raw_min_v
        )
        if psu_bounds_verified:
            psu_min_mv = int(raw_min_v * 1000)
            psu_max_mv = int(raw_max_v * 1000)
        else:
            logger.warning(
                "luxos %s returned missing or invalid PSU bounds (%r, %r); "
                "using unverified display fallback [11877, 15182] mV",
                self.ip,
                raw_min_v,
                raw_max_v,
            )
            psu_min_mv = 11877
            psu_max_mv = 15182

        raw = self._transport.send_cmd("frequencyget", "0")
        chips_per_board = int(raw["FREQS"][0]["Count"])

        raw = self._transport.send_cmd("devdetails")
        num_boards = len(raw["DEVDETAILS"]) or 3

        # Model-aware spec clamp. Currently only Antminer S21 has a hard-coded
        # spec range; future models slot in here as additional `elif` branches
        # against the manufacturer spec table.
        model = self._get_model_cached()
        if model and "S21" in model:
            s21_min, s21_max = 11877, 15182
            if psu_max_mv > s21_max or psu_min_mv < s21_min:
                logger.warning(
                    "luxos %s reports PSU range [%d, %d] mV; clamping to "
                    "Antminer S21 spec [%d, %d] mV (model=%r)",
                    self.ip,
                    psu_min_mv,
                    psu_max_mv,
                    s21_min,
                    s21_max,
                    model,
                )
                psu_min_mv = max(psu_min_mv, s21_min)
                psu_max_mv = min(psu_max_mv, s21_max)

        topo = HardwareTopology(
            num_boards=num_boards,
            chips_per_board=chips_per_board,
            psu_min_mv=psu_min_mv,
            psu_max_mv=psu_max_mv,
            psu_bounds_verified=psu_bounds_verified,
            psu_bounds_source=(
                "firmware:limits" if psu_bounds_verified else "fallback:static-spec"
            ),
        )
        with self._cache_lock:
            self._topology_cache = topo
        return topo

    def _get_model_cached(self) -> str | None:
        """Return the cached model string from ``version`` cmd, or None.

        Fetches ``VERSION[0]['Type']`` once per instance. Returns None on
        transport failure, missing field, or empty string — the model-aware
        clamp in ``hardware_topology()`` falls through to no-clamp behavior
        for unknown hardware. Best-effort: a transient failure does not
        cache None, so a later call retries.
        """
        with self._cache_lock:
            if self._model_cache is not None:
                return self._model_cache
        try:
            raw = self._transport.send_cmd("version")
        except Exception:  # noqa: BLE001
            return None
        version = raw.get("VERSION") or []
        if not version or not isinstance(version[0], dict):
            return None
        model_val = version[0].get("Type")
        if not isinstance(model_val, str) or not model_val.strip():
            return None
        model = model_val.strip()
        with self._cache_lock:
            self._model_cache = model
        return model
