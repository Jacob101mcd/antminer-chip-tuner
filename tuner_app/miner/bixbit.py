"""Whatsminer BixBit TCP/JSON API client — BixbitMinerAPI."""

from __future__ import annotations

import json
import logging
import socket

from tuner_app.miner.base import MinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.types import BoardSummary, HardwareTopology, MinerSummary
from tuner_app.net.response_limits import MINER_RECV_CHUNK_BYTES, append_capped_response

logger = logging.getLogger(__name__)


class BixbitMinerAPI(MinerAPI):
    """TCP/JSON socket client for Whatsminer BixBit miners.

    Wire format: json.dumps({"cmd": cmd_name, **params}) + "\n", encoded UTF-8.
    Reads 4096-byte chunks until JSON parses. Closes socket on exit.
    Single command per connection.
    """

    def __init__(self, ip, port=4028, password="letmein"):
        self.ip = ip
        self.port = port
        self.base = f"http://{ip}:{port}"
        self.password = password
        self._topology_cache = None

    def _send_cmd(self, cmd: str, **params) -> dict:
        """Send a JSON command to the Bixbit miner and return the parsed response.

        Raises:
            MinerOfflineError: TCP-level failure (timeout, refused, unreachable).
            MinerCommandError: STATUS=E response, or unparseable response body.
        """
        payload = {"cmd": cmd, **params}
        message = (json.dumps(payload) + "\n").encode("utf-8")
        try:
            with socket.create_connection((self.ip, self.port), timeout=15) as sock:
                sock.sendall(message)
                response = b""
                while True:
                    try:
                        chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                        if not chunk:
                            break
                        response = append_capped_response(response, chunk, command=cmd)
                        try:
                            result = json.loads(response.decode("utf-8"))
                            # Successful parse — got the full response
                            if isinstance(result, dict) and result.get("STATUS") == "E":
                                msg = result.get("Msg", "unknown error")
                                desc = result.get("Description", "")
                                raise MinerCommandError(f"{cmd}: {msg} ({desc})".strip(" ()"))
                            return result
                        except json.JSONDecodeError:
                            continue  # need more chunks
                    except TimeoutError:
                        break
                # Loop exited without parsing — broken or empty response
                raise MinerCommandError(f"{cmd}: incomplete response ({len(response)} bytes)")
        except (
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionError,
            socket.gaierror,
            TimeoutError,
            OSError,
        ) as e:
            raise MinerOfflineError(f"{cmd}: {e}") from e

    def _summary_raw(self):
        return self._send_cmd("summary")

    def summary(self) -> MinerSummary:
        return MinerSummary.from_bixbit(self._summary_raw())

    def clocks(self) -> list[BoardSummary]:
        return []

    def temps(self) -> list[BoardSummary]:
        return []

    def temps_chip(self) -> list[BoardSummary]:
        return []

    def hashrate(self) -> list[BoardSummary]:
        return []

    def capabilities(self):
        return self._send_cmd("get_firmware_version")

    def voltages(self):
        return self._send_cmd("get_overclock_info")

    def set_voltage(self, mv):
        self.hardware_topology().require_verified_voltage_target(mv)
        result = self._send_cmd("set_overclock_info", voltage_target=int(mv))
        if result.get("STATUS") == "S":
            return True
        raise MinerCommandError(f"set_voltage: unexpected status {result.get('STATUS')!r}")

    def set_clock_all(self, mhz):
        result = self._send_cmd("set_overclock_info", freq_target=int(mhz))
        if result.get("STATUS") == "S":
            return True
        raise MinerCommandError(f"set_clock_all: unexpected status {result.get('STATUS')!r}")

    def set_clock_board(self, board_clocks):
        raise NotImplementedError("per-board clock not supported on Bixbit")

    def set_clock_chip(self, board_index, chip_freqs):
        raise NotImplementedError("per-chip tuning not supported on Bixbit")

    def set_perpetualtune(self, enabled):
        # Bixbit auto-tunes internally via upfreq/profile system — no equivalent command.
        return True

    def set_coin(self, coin, stratum_configs, unique_id=False):
        raise NotImplementedError(
            "set_coin not supported on Bixbit (firmware is coin-fixed at SHA-256)"
        )

    def start_mining(self):
        result = self._send_cmd("power_on")
        if result.get("STATUS") == "S":
            return True
        raise MinerCommandError(f"start_mining: unexpected status {result.get('STATUS')!r}")

    def stop_mining(self):
        result = self._send_cmd("power_off")
        if result.get("STATUS") == "S":
            return True
        raise MinerCommandError(f"stop_mining: unexpected status {result.get('STATUS')!r}")

    def reboot(self, delay=0):
        # delay arg is accepted for API compatibility but ignored
        result = self._send_cmd("reboot")
        if result.get("STATUS") == "S":
            return True
        raise MinerCommandError(f"reboot: unexpected status {result.get('STATUS')!r}")

    def authenticate(self):
        try:
            result = self._send_cmd("summary")
            return result.get("STATUS") == "S"
        except (MinerOfflineError, MinerCommandError):
            return False

    def firmware_type(self) -> str:
        return "bixbit"

    def tuning_strategy(self) -> str:
        return "voltage_chip_tune"

    def set_power_limit(self, watts):
        result = self._send_cmd(
            "set_user_power_limit",
            powerLimit=int(watts),
            powerMode="Normal",
            softRestart=True,
        )
        if result.get("STATUS") == "S":
            return True
        raise MinerCommandError(f"set_power_limit: unexpected status {result.get('STATUS')!r}")

    def supports_per_chip_tuning(self) -> bool:
        return False

    def has_external_power_limit(self) -> bool:
        return True

    def has_capabilities_endpoint(self) -> bool:
        return False

    def has_internal_perpetual_tune(self) -> bool:
        return True

    def hardware_topology(self) -> HardwareTopology:
        if self._topology_cache is not None:
            return self._topology_cache

        try:
            response = self._send_cmd("get_board_slots_state")
            enabled = response.get("enabled")
            if not isinstance(enabled, list) or len(enabled) < 1:
                raise ValueError("Invalid enabled list")
            num_boards = len(enabled)
        except (MinerCommandError, MinerOfflineError, ValueError) as e:
            logger.warning("get_board_slots_state failed — using default 3 boards: %s", e)
            num_boards = 3

        topology = HardwareTopology(
            num_boards=num_boards,
            chips_per_board=0,
            psu_min_mv=11877,
            psu_max_mv=15182,
            psu_bounds_verified=False,
            psu_bounds_source="fallback:static-spec",
        )
        self._topology_cache = topology
        return topology
