"""ePIC UMC API client — EpicMinerAPI."""

from __future__ import annotations

import json
import logging
import math
import socket
import time

from tuner_app.miner.base import MinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerCommandPending, MinerOfflineError
from tuner_app.miner.types import BoardSummary, HardwareTopology, MinerSummary
from tuner_app.net.http_client import miner_http_request
from tuner_app.net.source_ip import clear_source_ip_cache

logger = logging.getLogger(__name__)


def _is_connection_error(exc):
    """True if `exc` is a network-level failure — as opposed to a protocol,
    JSON, or logic error from the miner itself. Used to distinguish
    OfflineError (pause) from CommandError (consume retry budget)."""
    return isinstance(
        exc,
        (
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            socket.timeout,
            socket.gaierror,
            TimeoutError,
            OSError,  # catches WinError 10060/10061, errno ENETUNREACH, etc.
        ),
    )


class EpicMinerAPI(MinerAPI):
    """HTTP client for ePIC UMC API on Antminer S21."""

    def __init__(self, ip, port=4028, password="letmein"):
        self.ip = ip
        self.port = port
        self.base = f"http://{ip}:{port}"
        self.password = password
        # /capabilities returns hardware info (Model, Model Subtype, Chip Type,
        # Default Clock, etc.) that's static for the lifetime of this client.
        # Cache it on first summary() call so the model field on MinerSummary
        # stays populated without re-fetching every time. Sentinel: None means
        # "not yet fetched"; {} means "fetched, missing or failed — don't retry".
        self._capabilities_cache: dict | None = None
        # /network exposes the canonical MAC at dhcp.mac_address (PowerPlay-BMS
        # firmware doesn't include MAC in /summary). Same caching shape as
        # _capabilities_cache: None = "not fetched"; {} = "fetched, missing
        # or failed — don't retry".
        self._network_cache: dict | None = None

    def _get(self, path):
        try:
            status, _, body = miner_http_request(self.ip, self.port, path, method="GET", timeout=15)
            if status != 200:
                return None
            return json.loads(body.decode())
        except Exception as e:
            # Network-level errors mean the miner is unreachable — surface that
            # as OfflineError so the engine can enter PHASE_OFFLINE. Other
            # failures (HTTP 4xx/5xx, malformed JSON) keep the quiet None
            # behavior the rest of the codebase depends on.
            if _is_connection_error(e):
                raise MinerOfflineError(f"GET {path}: {e}")  # noqa: B904
            return None

    def _post(self, path, param, retries=15, retry_delay=5, pending_retries=60):
        """Send POST command to miner. Raises MinerCommandError on failure.

        Two retry windows:
          * Connection/transport errors: `retries` attempts × `retry_delay`s (default 15 × 5 = 75s).
          * "Last command is still pending": `pending_retries` attempts × `retry_delay`s
            (default 60 × 5 = 300s).

        Pending is not a failure — the firmware is processing a prior command. The
        retry window is longer so the engine doesn't fall through to MinerCommandError
        during chip-by-chip ramps on 324 chips. When the pending window is exhausted,
        we raise `MinerCommandPending` (subclass of MinerCommandError) so the outer
        retry loop can wait-and-retry without invoking _attempt_miner_recovery."""
        body = json.dumps({"password": self.password, "param": param}).encode()
        last_connection_error = None
        pending_count = 0
        attempt = 0
        while True:
            try:
                status, _, resp_body = miner_http_request(
                    self.ip, self.port, path, data=body, method="POST", timeout=15
                )
                if status >= 400:
                    raise Exception(f"HTTP {status}")  # noqa: B017
                result = json.loads(resp_body.decode())
                if not result.get("result", False):
                    error_msg = str(result.get("error", "unknown error"))
                    if "pending" in error_msg.lower():
                        pending_count += 1
                        if pending_count < pending_retries:
                            time.sleep(retry_delay)
                            continue
                        raise MinerCommandPending(
                            f"POST {path} remained pending after "
                            f"{pending_count} × {retry_delay}s retries"
                        )
                    raise MinerCommandError(f"POST {path}: miner rejected command")
                return True
            except MinerCommandError:
                raise
            except Exception as e:
                # Connection failure may be a transient routing glitch — drop the
                # cached source IP so the next attempt re-probes interfaces.
                clear_source_ip_cache(self.ip)
                if _is_connection_error(e):
                    last_connection_error = e
                attempt += 1
                if attempt < retries:
                    time.sleep(retry_delay)
                    continue
                # Retries exhausted. If every failure was a network-level error
                # (no protocol response reached us), this is an offline
                # condition, not a command error.
                if last_connection_error is not None and _is_connection_error(e):
                    raise MinerOfflineError(f"POST {path}: {e}")  # noqa: B904
                raise MinerCommandError(f"POST {path} connection error: {e}")  # noqa: B904

    # GET endpoints
    def _summary_raw(self):
        return self._get("/summary")

    def _network_raw(self):
        return self._get("/network")

    def summary(self) -> MinerSummary:
        raw_summary = self._summary_raw()
        # Lazy-fetch /network into cache. PowerPlay-BMS firmware exposes the
        # canonical MAC at dhcp.mac_address; /summary does not include MAC.
        # Mirrors the _capabilities_cache pattern: catch network/command errors
        # and cache {} so subsequent summary() calls don't keep retrying.
        if self._network_cache is None:
            try:
                network_data = self._network_raw()
            except (MinerOfflineError, MinerCommandError):
                network_data = None
            self._network_cache = network_data if isinstance(network_data, dict) else {}
        result = MinerSummary.from_epic(raw_summary, raw_network=self._network_cache or None)
        # ePIC's /summary doesn't expose the hardware model — fetch it from
        # /capabilities once per client and cache. If /capabilities fails
        # (network blip, 404 on an older firmware), set the cache to {} so
        # future summary() calls don't keep retrying.
        if self._capabilities_cache is None:
            try:
                caps = self.capabilities()
            except (MinerOfflineError, MinerCommandError):
                caps = None
            self._capabilities_cache = caps if isinstance(caps, dict) else {}
        if result.model is None:
            cached_model = self._capabilities_cache.get("Model")
            if cached_model:
                result.model = cached_model
        return result

    def _clocks_raw(self):
        return self._get("/clocks")

    def clocks(self) -> list[BoardSummary]:
        raw = self._clocks_raw() or []
        boards = []
        for i, board in enumerate(raw):
            index = int(board.get("Index", i))
            chip_freqs = list(board.get("Data") or [])
            boards.append(
                BoardSummary(
                    index=index,
                    hashrate_ths=0.0,
                    freq_mhz=0.0,
                    chip_freqs_mhz=[float(f) for f in chip_freqs],
                )
            )
        return boards

    def _temps_raw(self):
        return self._get("/temps")

    def temps(self) -> list[BoardSummary]:
        boards = []
        for i, board in enumerate(self._temps_raw() or []):
            data = list(board.get("Data") or [])
            boards.append(
                BoardSummary(
                    index=int(board.get("Index", i)),
                    hashrate_ths=0.0,
                    freq_mhz=0.0,
                    temp_inlet_c=float(data[0]) if len(data) >= 1 else None,
                    temp_outlet_c=float(data[1]) if len(data) >= 2 else None,
                )
            )
        return boards

    def _temps_chip_raw(self):
        return self._get("/temps/chip")

    def temps_chip(self) -> list[BoardSummary]:
        boards = []
        for i, board in enumerate(self._temps_chip_raw() or []):
            data = list(board.get("Data") or [])
            boards.append(
                BoardSummary(
                    index=int(board.get("Index", i)),
                    hashrate_ths=0.0,
                    freq_mhz=0.0,
                    chip_temps_c=[float(t) for t in data],
                )
            )
        return boards

    def _hashrate_raw(self):
        return self._get("/hashrate")

    def hashrate(self) -> list[BoardSummary]:
        boards = []
        for i, board in enumerate(self._hashrate_raw() or []):
            chips = list(board.get("Data") or [])
            health_pct = [
                float(c[1]) for c in chips if isinstance(c, (list, tuple)) and len(c) >= 2
            ]
            hashrate_per_chip_mhs = [
                float(c[0]) / 1000.0 for c in chips if isinstance(c, (list, tuple)) and len(c) >= 1
            ]
            boards.append(
                BoardSummary(
                    index=int(board.get("Index", i)),
                    hashrate_ths=0.0,
                    freq_mhz=0.0,
                    health_pct=health_pct,
                    hashrate_per_chip_mhs=hashrate_per_chip_mhs,
                )
            )
        return boards

    def capabilities(self):
        return self._get("/capabilities")

    def hashrate_history(self):
        return self._get("/hashrate/history/continuous")

    def perpetualtune_status(self):
        return self._get("/perpetualtune")

    def voltages(self):
        return self._get("/voltages")

    # POST endpoints — all raise MinerCommandError on failure
    def set_voltage(self, mv):
        self.hardware_topology().require_verified_voltage_target(mv)
        return self._post("/tune/voltage", mv)

    def set_clock_all(self, mhz):
        return self._post("/tune/clock/all", float(mhz))

    def set_clock_board(self, board_clocks):
        v = [{"Index": idx, "Data": float(mhz)} for idx, mhz in board_clocks]
        return self._post("/tune/clock/board", {"v": v})

    def set_clock_chip(self, board_index, chip_freqs):
        data = [[chip_id, [float(freq)]] for chip_id, freq in chip_freqs]
        v = [{"Index": board_index, "Data": data}]
        return self._post("/tune/clock/chip", {"v": v})

    def set_perpetualtune(self, enabled):
        return self._post("/perpetualtune", enabled)

    def set_coin(self, coin, stratum_configs, unique_id=False):
        """POST /coin — update the miner's active coin + pool config. Used by
        both the bulk "Set Pools" flow and the MRR auto-publish pool config
        push. The ePIC API accepts `unique_id` as a nullable boolean for
        "Worker Unique Id" (visible in /summary as Stratum.Worker Unique Id).

        Args:
            coin: "BTC" or "LTC" (firmware enum).
            stratum_configs: list of dicts {pool, login, password}. Up to 3
                             entries; pool must be a full stratum URI like
                             "stratum+tcp://host:port[#xnsub]".
            unique_id: False to disable per-worker unique identifiers, True
                       to enable, None to leave unset.
        """
        param = {"coin": coin, "stratum_configs": stratum_configs}
        if unique_id is not None:
            param["unique_id"] = bool(unique_id)
        return self._post("/coin", param)

    def start_mining(self):
        return self._post("/miner", "Autostart")

    def stop_mining(self):
        return self._post("/miner", "Stop")

    def reboot(self, delay=0):
        return self._post("/reboot", delay)

    def authenticate(self):
        body = json.dumps({"password": self.password}).encode()
        try:
            status, _, resp_body = miner_http_request(
                self.ip, self.port, "/authenticate", data=body, method="POST", timeout=10
            )
            if status != 200:
                return False
            result = json.loads(resp_body.decode())
            return result.get("result", False)
        except Exception:  # noqa: BLE001
            return False

    def firmware_type(self) -> str:
        return "epic"

    def tuning_strategy(self) -> str:
        return "voltage_chip_tune"

    def set_power_limit(self, watts):
        """No-op on ePIC firmware — ePIC has no external power-limit knob.

        The abstract contract is satisfied; the method intentionally does nothing.
        BixbitMinerAPI maps this to set_user_power_limit.
        """
        pass

    def supports_per_chip_tuning(self) -> bool:
        return True

    def has_external_power_limit(self) -> bool:
        return False

    def has_capabilities_endpoint(self) -> bool:
        return True

    def has_internal_perpetual_tune(self) -> bool:
        return False

    def hardware_topology(self) -> HardwareTopology:
        if self._capabilities_cache is None:
            try:
                caps = self.capabilities()
            except (MinerOfflineError, MinerCommandError):
                caps = None
            self._capabilities_cache = caps if isinstance(caps, dict) else {}
        caps = self._capabilities_cache
        psu = caps.get("Psu Info") or {}
        psu_min_mv = psu.get("Min Vout")
        psu_max_mv = psu.get("Max Vout")
        psu_bounds_verified = (
            isinstance(psu_min_mv, (int, float))
            and not isinstance(psu_min_mv, bool)
            and isinstance(psu_max_mv, (int, float))
            and not isinstance(psu_max_mv, bool)
            and math.isfinite(float(psu_min_mv))
            and math.isfinite(float(psu_max_mv))
            and psu_min_mv >= 1000
            and psu_max_mv > psu_min_mv
        )
        if not psu_bounds_verified:
            logger.warning(
                "PSU bounds missing or invalid (min=%s, max=%s); using "
                "unverified display fallback [11877, 15182] mV",
                psu_min_mv,
                psu_max_mv,
            )
            psu_min_mv = 11877
            psu_max_mv = 15182
        perf = caps.get("Performance Estimator") or {}
        chips_per_board = perf.get("Chip Count", 108)
        num_boards = caps.get("Max HBs", 3)
        return HardwareTopology(
            num_boards=num_boards,
            chips_per_board=chips_per_board,
            psu_min_mv=int(psu_min_mv),
            psu_max_mv=int(psu_max_mv),
            psu_bounds_verified=psu_bounds_verified,
            psu_bounds_source=(
                "firmware:capabilities.Psu Info" if psu_bounds_verified else "fallback:static-spec"
            ),
        )
