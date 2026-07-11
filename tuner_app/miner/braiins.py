"""Braiins OS HTTP/JSON REST API client — BraiinsMinerAPI.

Implements the Braiins OS Public REST API v1.3.0 as documented at
https://developer.braiins-os.com/latest/openapi.json (saved to
/tmp/braiins-openapi-1.3.json for reference).

Auth: POST /api/v1/auth/login → {token, timeout_s}.  The token is passed
verbatim in the ``Authorization`` header of subsequent requests (no "Bearer"
prefix; the spec says "included in the `Authorization` header" with zero
occurrence of "Bearer" in the 86-kB spec document).

Default port: 80 (server URL in spec: ``http://miner/``).  # UNVERIFIED PORT
The existing per-miner API_PORT config key will override this at runtime.

Transport: reuses ``tuner_app.net.miner_http_request`` (source-IP-aware HTTP
client over stdlib ``http.client``).  No ``requests`` or ``grpcio`` dependency.
"""

from __future__ import annotations

import json
import logging
import time

from tuner_app.miner.base import MinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.types import BoardSummary, HardwareTopology, MinerSummary
from tuner_app.net.http_client import miner_http_request
from tuner_app.net.response_limits import read_capped_http_response

logger = logging.getLogger(__name__)


class BraiinsMinerAPI(MinerAPI):
    """HTTP/JSON REST client for Braiins OS miners.

    Wire format: JSON over HTTP (grpc-gateway REST shim).
    Auth: lazy token login; auto-refresh on HTTP 401 (one retry only).

    Token cache state:
        _token (str | None): current session token.
        _token_expires_at (float | None): monotonic time when token expires.
    """

    # Default port per spec server URL ``http://miner/`` (implicit port 80).
    # UNVERIFIED PORT — confirm against a live Braiins miner; override via
    # API_PORT per-miner config if the miner runs on a non-standard port.
    DEFAULT_PORT = 80

    def __init__(self, ip: str, port: int = DEFAULT_PORT, password: str = "letmein"):
        self.ip = ip
        self.port = port
        self.base = f"http://{ip}:{port}"
        self.password = password
        # Token cache — populated on first authenticated request.
        self._token: str | None = None
        self._token_expires_at: float | None = None
        # Username used for login — overrideable via BRAIINS_USERNAME config.
        # Engine reads engine.config.get("BRAIINS_USERNAME", "root") and passes
        # it here via the constructor (Run 2 wires this up).  Default "root"
        # matches the spec's example: {"username": "root", "password": "1234"}.
        self.username: str = "root"
        # Topology cache — populated on first hardware_topology() call.
        self._topology_cache: HardwareTopology | None = None

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _raw_request(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        extra_headers: dict | None = None,
    ) -> tuple[int, bytes]:
        """Send an HTTP request to the miner.  Returns (status_code, body_bytes).

        Does NOT inject the Authorization header — callers that need auth use
        ``_authed_request`` instead.  Raises ``MinerOfflineError`` on any
        connection-level failure.
        """
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        # miner_http_request signature:
        # (ip, port, path, data, method, timeout, *, source_ip) → (status, headers, body)
        try:
            status, _headers, resp_body = miner_http_request(
                self.ip,
                self.port,
                path,
                data=data,
                method=method,
                timeout=15,
            )
        except OSError as exc:
            raise MinerOfflineError(f"{method} {path}: {exc}") from exc

        return status, resp_body

    def _authed_request(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
    ) -> tuple[int, bytes]:
        """Send an authenticated request.  Auto-logs-in if no token cached.

        On HTTP 401 (token expired mid-session), performs one re-login and
        retries.  Raises ``MinerOfflineError`` on connection failure;
        ``MinerCommandError`` on persistent auth failure.
        """
        self._ensure_token()
        status, resp_body = self._raw_request_with_token(path, method, body)
        if status == 401:
            # Token expired — refresh and retry once.
            self._token = None
            self._token_expires_at = None
            self._ensure_token()
            status, resp_body = self._raw_request_with_token(path, method, body)
        if status == 401:
            raise MinerCommandError(f"{method} {path}: HTTP 401 after token refresh")
        return status, resp_body

    def _raw_request_with_token(
        self,
        path: str,
        method: str,
        body: dict | None,
    ) -> tuple[int, bytes]:
        """Like ``_raw_request`` but injects ``Authorization: <token>`` header.

        Uses the low-level http.client approach inside ``miner_http_request``
        by encoding the auth header through the data payload workaround — but
        ``miner_http_request`` doesn't accept custom headers yet.  We therefore
        call it through a thin wrapper that adds the header at the
        http.client level.

        NOTE: ``miner_http_request`` in ``tuner_app/net/http_client.py`` only
        sets ``Content-Type: application/json`` when ``data`` is not None.
        It doesn't support custom headers.  We replicate its logic here,
        adding the Authorization header, rather than reimplementing the helper.
        """
        import http.client

        from tuner_app.net.source_ip import resolve_source_ip

        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        source_ip = resolve_source_ip(self.ip, self.port)
        source_address = (source_ip, 0) if source_ip else None
        conn = http.client.HTTPConnection(
            self.ip, self.port, timeout=15, source_address=source_address
        )
        try:
            headers: dict[str, str] = {}
            if data is not None:
                headers["Content-Type"] = "application/json"
            # OpenAPI 1.3.0 — /api/v1/auth/login description:
            # "a token is returned, which should be included in the
            # `Authorization` header of subsequent requests."
            # No "Bearer" prefix; no securitySchemes block in the spec.
            if self._token:
                headers["Authorization"] = self._token
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp_body = read_capped_http_response(resp)
            return status, resp_body
        except OSError as exc:
            raise MinerOfflineError(f"{method} {path}: {exc}") from exc
        finally:
            try:  # noqa: SIM105
                conn.close()
            except Exception:
                pass

    def _ensure_token(self) -> None:
        """Login if we have no valid cached token.

        OpenAPI 1.3.0 — POST /api/v1/auth/login
        Request body schema (components.schemas.LoginRequest):
          {"username": "string", "password": "string"}
          Required: ["username", "password"]
        Response schema (components.schemas.LoginResponse):
          {"token": "string", "timeout_s": "integer (int32, min 0)"}
          Required: ["token", "timeout_s"]
          timeout_s description: "Token validity refreshed to this value with
            each request."
        Auth: none required on this endpoint.
        HTTP 200 on success.
        """
        now = time.monotonic()
        if self._token is not None and (
            self._token_expires_at is None or now < self._token_expires_at
        ):
            return  # token still valid

        status, resp_body = self._raw_request(
            "/api/v1/auth/login",
            method="POST",
            body={"username": self.username, "password": self.password},
        )
        if status != 200:
            raise MinerCommandError(f"POST /api/v1/auth/login: HTTP {status}")
        try:
            resp = json.loads(resp_body)
        except json.JSONDecodeError as exc:
            raise MinerCommandError("POST /api/v1/auth/login: invalid JSON response") from exc
        token = resp.get("token")
        timeout_s = resp.get("timeout_s", 3600)
        if not token:
            raise MinerCommandError("POST /api/v1/auth/login: response missing token")
        self._token = token
        # Expire the cached token 60 s before its server-side expiry to avoid
        # racing a server-side invalidation.
        self._token_expires_at = now + max(0, int(timeout_s) - 60)

    def _get_json(self, path: str) -> dict:
        """Perform an authenticated GET and return parsed JSON body.

        Raises:
            MinerOfflineError: on connection failure.
            MinerCommandError: on non-2xx HTTP status or JSON parse failure.
        """
        status, resp_body = self._authed_request(path, method="GET")
        if not (200 <= status < 300):
            raise MinerCommandError(f"GET {path}: HTTP {status}")
        try:
            return json.loads(resp_body) if resp_body else {}
        except json.JSONDecodeError as exc:
            raise MinerCommandError(f"GET {path}: invalid JSON response") from exc

    def _put_json(self, path: str, body: dict) -> dict:
        """Perform an authenticated PUT with JSON body, return parsed response.

        Raises:
            MinerOfflineError: on connection failure.
            MinerCommandError: on non-2xx HTTP status or JSON parse failure.
        """
        status, resp_body = self._authed_request(path, method="PUT", body=body)
        if not (200 <= status < 300):
            raise MinerCommandError(f"PUT {path}: HTTP {status}")
        if not resp_body:
            return {}
        try:
            return json.loads(resp_body)
        except json.JSONDecodeError as exc:
            raise MinerCommandError(f"PUT {path}: invalid JSON response") from exc

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def firmware_type(self) -> str:
        return "braiins"

    def tuning_strategy(self) -> str:
        return "wattage_search"

    def supports_per_chip_tuning(self) -> bool:
        """BOS owns V/F via internal AutoTune — per-chip tuning N/A."""
        return False

    def has_external_power_limit(self) -> bool:
        """PUT /api/v1/performance/power-target is BOS's documented power-limit knob."""
        return True

    def has_capabilities_endpoint(self) -> bool:
        """BOS has no ePIC-style /capabilities endpoint."""
        return False

    def has_internal_perpetual_tune(self) -> bool:
        """BOS AutoTune is the internal perpetual tuner."""
        return True

    def hardware_topology(self) -> HardwareTopology:
        """Return the miner's hardware topology, cached for the instance lifetime.

        Queries GET /api/v1/configuration/constraints (the same endpoint as
        capabilities()) and reads hashboards_constraints to determine num_boards.

        Falls back to 3 boards if the constraints response is missing, malformed,
        or has a null/empty hashboards_constraints field.

        chips_per_board=0 sentinel: BOS owns V/F internally; no per-chip API.
        PSU bounds: hardcoded from the S21 PSU Type 193 spec (REST API does not
        expose PSU output range).
        """
        if self._topology_cache is not None:
            return self._topology_cache

        num_boards = 3  # default fallback

        try:
            constraints = self._get_json("/api/v1/configuration/constraints")
            # UNVERIFIED — pending live verification of HashboardConstraints schema.
            # The OpenAPI 1.3.0 spec declares hashboards_constraints as
            # HashboardConstraints | null but does not document the inner shape.
            # Try common field names; fall back to default on any miss.
            hb_constraints = (
                constraints.get("hashboards_constraints") if isinstance(constraints, dict) else None
            )
            if isinstance(hb_constraints, dict):
                count = (
                    hb_constraints.get("count")
                    or hb_constraints.get("max_count")
                    or hb_constraints.get("num_hashboards")
                )
                if isinstance(count, int) and count > 0:
                    num_boards = count
        except (MinerCommandError, MinerOfflineError) as e:
            logger.warning(
                "hardware_topology: constraints fetch failed (%s); using default 3 boards", e
            )

        topology = HardwareTopology(
            num_boards=num_boards,
            chips_per_board=0,
            psu_min_mv=11877,
            psu_max_mv=15182,
            psu_bounds_verified=False,
            psu_bounds_source="not-applicable:firmware-owned-vf",
        )
        self._topology_cache = topology
        return topology

    def summary(self) -> MinerSummary:
        """Synthesize a MinerSummary from /miner/details + /miner/stats + /cooling/state.

        See ``MinerSummary.from_braiins`` for field mapping.
        """
        raw_details = self._summary_details_raw()
        raw_stats = self._summary_stats_raw()
        raw_cooling = self._summary_cooling_raw()
        return MinerSummary.from_braiins(raw_details, raw_stats, raw_cooling)

    def _summary_details_raw(self) -> dict:
        """GET /api/v1/miner/details — hardware identity and status.

        OpenAPI 1.3.0 — GET /api/v1/miner/details
        Response schema (components.schemas.GetMinerDetailsResponse):
          Required: ["uid","platform","bos_mode","hostname","mac_address",
                     "system_uptime","bosminer_uptime_s","system_uptime_s",
                     "status","kernel_version","control_board_soc_family"]
          Optional:
            bos_version: {current: str, major: str, bos_plus: bool}
            miner_identity: {brand: int32, miner_model: str, name: str,
                             model: int32 (deprecated)}
            serial_number: str | null
            sticker_hashrate: {gigahash_per_second: float}
            psu_info: {model_name, serial_number, version, fw_version,
                       min_voltage, max_voltage}
          status field: int32 representing one of: unspecified, not_started,
            normal, paused, suspended, restricted (no int→string enum in spec).
        Auth: required (Authorization: <token>).
        """
        return self._get_json("/api/v1/miner/details")

    def _summary_stats_raw(self) -> dict:
        """GET /api/v1/miner/stats — hashrate and power statistics.

        OpenAPI 1.3.0 — GET /api/v1/miner/stats
        Response schema (components.schemas.GetMinerStatsResponse):
          miner_stats: WorkSolverStats | null
            found_blocks: int32
            nominal_hashrate: {gigahash_per_second: float} | null
            real_hashrate: RealHashrate | null
              last_5s, last_1m, last_5m, last_15m, last_30m, last_1h,
              last_24h, last_15s, last_30s, since_restart:
                each is {gigahash_per_second: float} | null
            error_hashrate: {megahash_per_second: float} | null
          pool_stats: PoolStats | null
          power_stats: MinerPowerStats | null
            approximated_consumption: {watt: int64} | null
            efficiency: {joule_per_terahash: float} | null
        Auth: required.
        """
        return self._get_json("/api/v1/miner/stats")

    def _summary_cooling_raw(self) -> dict:
        """GET /api/v1/cooling/state — fan speeds and highest temperature.

        OpenAPI 1.3.0 — GET /api/v1/cooling/state
        Response schema (components.schemas.GetCoolingStateResponse):
          Required: ["fans"]
          fans: array of FanState
            FanState: {position: int32|null, rpm: int32, target_speed_ratio: float|null}
          highest_temperature: TemperatureSensor | null
            TemperatureSensor: {location: int32, id: int32|null,
                                temperature: {degree_c: float} | null}
        Auth: required.
        """
        return self._get_json("/api/v1/cooling/state")

    def clocks(self) -> list[BoardSummary]:
        """Braiins OS exposes no per-chip clock array API — returns []."""
        return []

    def temps(self) -> list[BoardSummary]:
        """Braiins OS exposes no per-board inlet/outlet temp API — returns []."""
        return []

    def temps_chip(self) -> list[BoardSummary]:
        """Braiins OS exposes no per-chip temperature API — returns []."""
        return []

    def hashrate(self) -> list[BoardSummary]:
        """Braiins OS exposes no per-chip hashrate API — returns []."""
        return []

    def capabilities(self) -> dict:
        """GET /api/v1/configuration/constraints — miner capability constraints.

        OpenAPI 1.3.0 — GET /api/v1/configuration/constraints
        Response schema (components.schemas.GetConstraintsResponse):
          cooling_constraints: CoolingConstraints | null
          dps_constraints: DpsConstraints | null
          hashboards_constraints: HashboardConstraints | null
          tuner_constraints: TunerConstraints | null
        Auth: required.
        Returns raw dict for engine/UI consumption; no DTO translation needed
        for Run 1 (same pattern as BixbitMinerAPI.capabilities).
        """
        return self._get_json("/api/v1/configuration/constraints")

    def voltages(self) -> dict:
        """GET /api/v1/performance/mode — current performance mode config.

        Braiins OS does not expose a dedicated voltage endpoint. The closest
        equivalent is the performance mode, which contains per-hashboard
        frequency and voltage settings in manual mode.

        OpenAPI 1.3.0 — GET /api/v1/performance/mode
        Response schema (components.schemas.PerformanceModeMode):
          oneOf:
            {manualmode: ManualPerformanceMode}
              ManualPerformanceMode:
                global_frequency: {hertz: float} | null
                global_voltage: {volt: float} | null
                hashboards: array of HashboardPerformanceSettings
                  {id: str, frequency: {hertz: float}|null, voltage: {volt: float}|null}
            {tunermode: TunerPerformanceMode}
              TunerPerformanceMode:
                target: TunerPerformanceModeTarget | null
                  oneOf:
                    {powertarget: PowerTargetMode {power_target: {watt: int64}|null}}
                    {hashratetarget: HashrateTargetMode
                       {hashrate_target: {tera_hash_per_second: float}|null}}
        Auth: required.
        """
        return self._get_json("/api/v1/performance/mode")

    def set_voltage(self, mv: float) -> None:
        """Not supported: Braiins OS does not expose a voltage-set knob via the REST API.

        Voltage is managed internally by the BOS tuner. Use ``set_power_limit``
        or ``set_perpetualtune`` to influence operating point.
        """
        raise NotImplementedError(
            "set_voltage not supported on Braiins OS (voltage managed internally by BOS tuner)"
        )

    def set_clock_all(self, mhz: float) -> None:
        """Not supported: Braiins OS does not expose a direct global clock-set knob."""
        raise NotImplementedError(
            "set_clock_all not supported on Braiins OS (use set_power_limit or set_perpetualtune)"
        )

    def set_clock_board(self, board_clocks) -> None:
        """Not supported: Braiins OS does not expose per-board clock-set knobs."""
        raise NotImplementedError("set_clock_board not supported on Braiins OS")

    def set_clock_chip(self, board_index, chip_freqs) -> None:
        """Not supported: Braiins OS does not expose per-chip clock-set knobs."""
        raise NotImplementedError("set_clock_chip not supported on Braiins OS")

    def set_coin(self, coin, stratum_configs, unique_id=False) -> None:
        """Not supported: Braiins OS firmware is SHA-256 only (no coin-switch API)."""
        raise NotImplementedError(
            "set_coin not supported on Braiins OS (firmware is SHA-256 fixed)"
        )

    def set_perpetualtune(self, enabled: bool) -> dict:
        """Switch between tuner mode (auto) and manual mode via /api/v1/performance/mode.

        When enabled=True → tunermode with powertarget target (tuner manages
        the operating point automatically).
        When enabled=False → manualmode with empty hashboards array (BOS
        retains current per-hashboard settings).

        OpenAPI 1.3.0 — PUT /api/v1/performance/mode
        Request body schema (components.schemas.PerformanceModeMode):
          oneOf:
            {tunermode: {target: {powertarget: {power_target: {watt: int64}|null}}}}
            {manualmode: {hashboards: []}}
        Response schema: same as request body (PerformanceModeMode).
        Auth: required.
        HTTP 200 on success.

        Note: When switching to tunermode, the power target is left as null
        so BOS uses whatever power target was last configured.  The engine's
        braiins_phases.py sets an explicit power target via set_power_limit()
        before calling this method when it needs a specific wattage.
        """
        if enabled:
            body: dict = {"tunermode": {"target": {"powertarget": {"power_target": None}}}}
        else:
            body = {"manualmode": {"hashboards": []}}
        return self._put_json("/api/v1/performance/mode", body)

    def set_power_limit(self, watts: int) -> dict:
        """Set power consumption target in watts via /api/v1/performance/power-target.

        OpenAPI 1.3.0 — PUT /api/v1/performance/power-target
        Request body schema (components.schemas.Power):
          Required: ["watt"]
          {"watt": integer (int64, minimum 0)}
        Response schema: same Power object.
          {"watt": integer}
        Example from spec: {"watt": 3730}
        Auth: required.
        HTTP 200 on success, returns the applied Power value.
        """
        return self._put_json("/api/v1/performance/power-target", {"watt": int(watts)})

    def start_mining(self) -> bool:
        """Start mining via PUT /api/v1/actions/start.

        OpenAPI 1.3.0 — PUT /api/v1/actions/start
        Request body: none.
        Response schema: boolean
          "Successful response containing bool if was already running."
        Auth: required.
        HTTP 200 on success.
        Returns True if already running, False if newly started.
        Raises MinerCommandError on non-2xx response.
        """
        status, resp_body = self._authed_request("/api/v1/actions/start", method="PUT")
        if not (200 <= status < 300):
            raise MinerCommandError(f"PUT /api/v1/actions/start: HTTP {status}")
        try:
            return bool(json.loads(resp_body)) if resp_body else False
        except (json.JSONDecodeError, ValueError):
            return False

    def stop_mining(self) -> bool:
        """Stop mining via PUT /api/v1/actions/stop.

        OpenAPI 1.3.0 — PUT /api/v1/actions/stop
        Request body: none.
        Response schema: boolean
          "Successful response containing bool if was already stopped."
        Auth: required.
        HTTP 200 on success.
        Returns True if already stopped, False if newly stopped.
        Raises MinerCommandError on non-2xx response.
        """
        status, resp_body = self._authed_request("/api/v1/actions/stop", method="PUT")
        if not (200 <= status < 300):
            raise MinerCommandError(f"PUT /api/v1/actions/stop: HTTP {status}")
        try:
            return bool(json.loads(resp_body)) if resp_body else False
        except (json.JSONDecodeError, ValueError):
            return False

    def reboot(self, delay: int = 0) -> None:
        """Reboot the miner via PUT /api/v1/actions/reboot.

        OpenAPI 1.3.0 — PUT /api/v1/actions/reboot
        Request body: none.
        Response: 204 No Content on success (no body).
          "Successful response."
        Auth: required.
        The ``delay`` arg is accepted for API compatibility but ignored
        (Braiins OS reboot has no delay parameter in the spec).
        Raises MinerCommandError on non-2xx/204 response.
        """
        status, resp_body = self._authed_request("/api/v1/actions/reboot", method="PUT")
        if not (200 <= status < 300):
            raise MinerCommandError(f"PUT /api/v1/actions/reboot: HTTP {status}")

    def authenticate(self) -> bool:
        """Perform a login round-trip; return True on success, False on failure.

        Clears the cached token first so a fresh login is always attempted.
        Returns False on auth failure (HTTP 4xx) or offline (MinerOfflineError).
        """
        self._token = None
        self._token_expires_at = None
        try:
            self._ensure_token()
            return self._token is not None
        except (MinerOfflineError, MinerCommandError):
            return False
