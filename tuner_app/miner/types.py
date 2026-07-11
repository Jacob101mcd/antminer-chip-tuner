from __future__ import annotations

import dataclasses
import math
import re

from tuner_app.miner.exceptions import UnsafeVoltageBoundsError


def _extract_canonical_mac(raw: object) -> str | None:
    """Return a canonical lowercase colon-separated MAC string, or None.

    Accepts colon-separated, dash-separated, or bare 12-hex input in any case.
    Returns None for empty strings, all-zeros placeholders, malformed input,
    and any non-string type. Never raises.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    # Detect bare 12-hex form and insert colons.
    if re.fullmatch(r"[0-9a-fA-F]{12}", s):
        s = ":".join(s[i : i + 2] for i in range(0, 12, 2))
    # Now must be colon- or dash-separated 6 hex pairs.
    if not re.fullmatch(r"[0-9a-fA-F]{2}([:-][0-9a-fA-F]{2}){5}", s):
        return None
    canonical = s.replace("-", ":").lower()
    if canonical == "00:00:00:00:00:00":
        return None
    return canonical


@dataclasses.dataclass
class BoardSummary:
    index: int
    hashrate_ths: float
    freq_mhz: float
    target_voltage_mv: float | None = None
    temp_inlet_c: float | None = None
    temp_outlet_c: float | None = None
    board_health_pct: float | None = None
    chip_freqs_mhz: list[float] = dataclasses.field(default_factory=list)
    chip_temps_c: list[float] = dataclasses.field(default_factory=list)
    health_pct: list[float] = dataclasses.field(default_factory=list)
    hashrate_per_chip_mhs: list[float] = dataclasses.field(default_factory=list)
    upfreq_complete: bool | None = None
    effective_chips: int | None = None


@dataclasses.dataclass
class MinerSummary:
    operating_state: str
    hashrate_ths: float
    power_w: float
    fan_speed: int
    target_voltage_mv: float | None = None
    output_voltage_mv: float | None = None
    hostname: str | None = None
    model: str | None = None
    mac: str | None = None
    boards: list[BoardSummary] = dataclasses.field(default_factory=list)
    raw: dict = dataclasses.field(default_factory=dict)

    @property
    def is_hashing(self) -> bool:
        return self.hashrate_ths > 0 or any(b.hashrate_ths > 0 for b in self.boards)

    @classmethod
    def from_epic(cls, raw: dict, raw_network: dict | None = None) -> MinerSummary:
        def _board_health(hb: dict) -> float | None:
            hr = hb.get("Hashrate") or []
            return float(hr[2]) if len(hr) >= 3 else None

        status = raw.get("Status")
        operating_state = status.get("Operating State", "") if isinstance(status, dict) else ""
        psu = raw.get("Power Supply Stats", {}) or {}
        target_v = psu.get("Target Voltage", 0)
        target_voltage_mv = float(target_v) if target_v > 0 else None
        out_v = psu.get("Output Voltage", 0)
        output_voltage_mv = float(out_v) * 1000 if out_v > 0 else None
        # ePIC firmware (PowerPlay-BMS v1.17.x as of 2026-05) returns Hostname
        # at the top level of /summary, not nested under Network. Older / newer
        # variants may use Network.Hostname — fall back for forward compat.
        hostname_val = raw.get("Hostname") or raw.get("Network", {}).get("Hostname")
        hostname = hostname_val if hostname_val else None
        # /summary doesn't expose the hardware model — it lives at /capabilities.
        # EpicMinerAPI.summary() populates this field after from_epic() returns.
        # Top-level Type/Model/MinerType fallbacks kept defensively for any
        # variant that does include them inline.
        model = raw.get("Type") or raw.get("Model") or raw.get("MinerType") or None
        boards = [
            BoardSummary(
                index=int(hb.get("Index", i)),
                hashrate_ths=float(hb.get("Hashrate", [0])[0]) / 1e6,
                freq_mhz=float(hb.get("Core Clock Avg", 0) or 0),
                target_voltage_mv=float(hb["Input Voltage"]) if "Input Voltage" in hb else None,
                board_health_pct=_board_health(hb),
            )
            for i, hb in enumerate(raw.get("HBs", []))
        ]
        # MAC: PowerPlay-BMS firmware exposes the canonical MAC at /network's
        # dhcp.mac_address. The summary endpoint does not include MAC. The
        # raw_network kwarg holds the parsed /network response when available.
        mac = None
        if isinstance(raw_network, dict):
            dhcp = raw_network.get("dhcp")
            if isinstance(dhcp, dict):
                mac = _extract_canonical_mac(dhcp.get("mac_address"))
        return cls(
            operating_state=operating_state,
            hashrate_ths=sum(hb.get("Hashrate", [0])[0] for hb in raw.get("HBs", [])) / 1e6,
            power_w=float(psu.get("Input Power", 0) or 0),
            fan_speed=int(raw.get("Fans", {}).get("Fans Speed", 0) or 0),
            target_voltage_mv=target_voltage_mv,
            output_voltage_mv=output_voltage_mv,
            hostname=hostname,
            model=model,
            mac=mac,
            boards=boards,
            raw=raw,
        )

    @classmethod
    def from_bixbit(cls, raw: dict) -> MinerSummary:
        status = raw.get("Status", "")
        operating_state = str(status) if not isinstance(status, dict) else ""
        psu_vout = raw.get("PSU Vout", 0)
        output_voltage_mv = float(psu_vout) * 1000 if psu_vout > 0 else None
        model_val = raw.get("Miner Type")
        model = model_val if model_val else None
        # MAC: defensive lookup chain. Whatsminer docs document only reset_mac
        # (a setter); no GET cmd is documented. Most fixtures yield None.
        mac = (
            _extract_canonical_mac(raw.get("MAC"))
            or _extract_canonical_mac(raw.get("MACAddr"))
            or _extract_canonical_mac(raw.get("MAC Address"))
        )
        return cls(
            operating_state=operating_state,
            hashrate_ths=float(raw.get("HS RT", 0) or 0) / 1e6,
            power_w=float(raw.get("Power Realtime", raw.get("Power", 0)) or 0),
            fan_speed=int(raw.get("Fan Speed Out", 0) or 0),
            target_voltage_mv=None,
            output_voltage_mv=output_voltage_mv,
            hostname=None,
            model=model,
            mac=mac,
            boards=[],
            raw=raw,
        )

    @classmethod
    def from_whatsminer(
        cls,
        raw_summary: dict,
        raw_devs: dict | None = None,
        raw_version: dict | None = None,
        raw_miner_info: dict | None = None,
    ) -> MinerSummary:
        # Three btminer summary response shapes are tolerated:
        #
        #   1. btminer wrapped:   {"STATUS":"S","Code":131,"Msg":{...fields...}}
        #      The H616-platform M-series firmware (e.g. M66S++_VM30 fw
        #      20251209.16.Rel3, api_ver 2.2.2) returns this — every summary
        #      field is nested under `Msg`. There is no top-level `SUMMARY`
        #      key here, so the cgminer detection below falls through.
        #
        #   2. cgminer-style:     {"STATUS":[{...}], "SUMMARY":[{...fields...}]}
        #      Older btminer + some H6-class boards return this. SUMMARY[0]
        #      is a dict of the same fields.
        #
        #   3. flat top-level:    {"STATUS":"S","MHS av":..., "Power":...}
        #      Test fixtures + hypothetical inline-flat firmware. Used as the
        #      ultimate fallback so explicit inline test bodies still work.
        #
        # Optional auxiliary dicts:
        #   - raw_version: result of `get_version` cmd. Used to source the
        #     `miner_type` field when the summary's `Miner Type` is missing
        #     (H616 stock firmware omits it from the summary body).
        #   - raw_miner_info: result of `get_miner_info` cmd. Source for
        #     hostname + MAC on btminer firmware (these aren't in summary
        #     on H616-class boards).
        #
        # The `raw` field on the returned DTO is always the full original
        # response (raw_summary), not the unwrapped subset.
        msg = raw_summary.get("Msg")
        summary_arr = raw_summary.get("SUMMARY")
        if isinstance(msg, dict):
            s = msg
        elif isinstance(summary_arr, list) and summary_arr and isinstance(summary_arr[0], dict):
            s = summary_arr[0]
        else:
            s = raw_summary
        # H616 btminer response uses the STATUS wrapper ("S"/"E") to signal
        # cmd success; the summary body itself doesn't carry an explicit
        # "Mining" string. Derive operating_state from STATUS + hashrate so
        # the engine sees Mining / Idle / Offline rather than the literal
        # "S"/"E" string. Pre-H616 + flat shapes that set "Status" inline
        # fall back to the old behavior via the elif branch.
        hashrate_ths = float(s.get("MHS av", 0) or 0) / 1e6
        legacy_status = raw_summary.get("Status", "")
        if raw_summary.get("STATUS") in ("S", "E"):
            operating_state = (
                "Mining"
                if (raw_summary.get("STATUS") == "S" and hashrate_ths > 0)
                else "Idle"
                if raw_summary.get("STATUS") == "S"
                else "Offline"
            )
        elif isinstance(legacy_status, dict):
            operating_state = ""
        else:
            operating_state = str(legacy_status)
        power_w = float(s.get("Power", 0) or 0)
        fan_speed = int(s.get("Fan Speed Out", s.get("Fan Speed In", 0)) or 0)
        # Model: prefer get_version's miner_type (H616 source of truth);
        # fall back to the summary's Miner Type (cgminer-style firmwares).
        version_msg = raw_version.get("Msg") if isinstance(raw_version, dict) else None
        model = (version_msg.get("miner_type") if isinstance(version_msg, dict) else None) or s.get(
            "Miner Type"
        )
        # Hostname + MAC: H616 lacks both in summary; get_miner_info has them.
        miner_info_msg = raw_miner_info.get("Msg") if isinstance(raw_miner_info, dict) else None
        hostname = miner_info_msg.get("hostname") if isinstance(miner_info_msg, dict) else None
        mac = (
            _extract_canonical_mac(
                miner_info_msg.get("mac") if isinstance(miner_info_msg, dict) else None
            )
            or _extract_canonical_mac(s.get("MAC"))
            or _extract_canonical_mac(s.get("MACAddr"))
            or _extract_canonical_mac(s.get("MAC Address"))
        )
        boards = []
        if raw_devs is not None:
            for i, d in enumerate(raw_devs.get("DEVS", [])):
                slot = d.get("Slot", i)
                hashrate_ths_board = float(d.get("MHS av", 0) or 0) / 1e6
                freq_mhz = float(d.get("Chip Frequency", 0) or 0)
                temp_outlet_c = d.get("Temperature")
                if temp_outlet_c is None or temp_outlet_c == 0:
                    temp_outlet_c = None
                upfreq_complete = bool(d.get("Upfreq Complete", 0))
                effective_chips = d.get("Effective Chips")
                if effective_chips is not None:
                    effective_chips = int(effective_chips) if effective_chips != 0 else None
                board = BoardSummary(
                    index=slot,
                    hashrate_ths=hashrate_ths_board,
                    freq_mhz=freq_mhz,
                    temp_outlet_c=temp_outlet_c,
                    upfreq_complete=upfreq_complete,
                    effective_chips=effective_chips,
                )
                boards.append(board)
        return cls(
            operating_state=operating_state,
            hashrate_ths=hashrate_ths,
            power_w=power_w,
            fan_speed=fan_speed,
            target_voltage_mv=None,
            output_voltage_mv=None,
            hostname=hostname,
            model=model,
            mac=mac,
            boards=boards,
            raw=raw_summary,
        )

    @classmethod
    def from_braiins(
        cls,
        raw_details: dict,
        raw_stats: dict,
        raw_cooling: dict,
    ) -> MinerSummary:
        """Synthesize a MinerSummary from three Braiins OS REST endpoints.

        Args:
            raw_details: Response body from GET /api/v1/miner/details
                (components.schemas.GetMinerDetailsResponse).
            raw_stats: Response body from GET /api/v1/miner/stats
                (components.schemas.GetMinerStatsResponse).
            raw_cooling: Response body from GET /api/v1/cooling/state
                (components.schemas.GetCoolingStateResponse).

        The ``raw`` field on the returned DTO is set to raw_details (the
        primary identity document).  Callers needing stats or cooling data
        should call the raw endpoints directly.

        Field mapping (OpenAPI 1.3.0, spec saved at /tmp/braiins-openapi-1.3.json):

        operating_state:
            Braiins OS /api/v1/miner/details → ``status`` (int32).
            Integer status codes described in /api/v1/miner/status streaming
            endpoint: unspecified=0, not_started, normal, paused, suspended,
            restricted.  No int→string enum is defined in the spec; we map
            common values and fall back to the raw int string.
            STATUS_MAP = {0:"unspecified", 1:"not_started", 2:"normal",
                          3:"paused", 4:"suspended", 5:"restricted"}

        hashrate_ths:
            /api/v1/miner/stats → miner_stats (WorkSolverStats) →
            real_hashrate (RealHashrate) → last_1m (GigaHashrate) →
            gigahash_per_second.  Divide GH/s by 1000 to get TH/s.
            Falls back to last_5m, last_15m, nominal_hashrate in order.
            If miner_stats is null, returns 0.0.

        power_w:
            /api/v1/miner/stats → power_stats (MinerPowerStats) →
            approximated_consumption (Power) → watt (int64).
            Returns 0.0 if power_stats or approximated_consumption is null.

        fan_speed:
            /api/v1/cooling/state → fans (array of FanState) → first
            element's rpm (int32).  Returns 0 if fans array is empty.
            FanState schema: {position: int32|null, rpm: int32,
                              target_speed_ratio: float|null}

        target_voltage_mv:
            Always None — Braiins OS does not expose voltage in the
            summary-level endpoints.

        output_voltage_mv:
            Always None — not available in summary endpoints.

        hostname:
            /api/v1/miner/details → hostname (str, required field).

        mac:
            /api/v1/miner/details → mac_address (str, required field per
            OpenAPI 1.3.0 GetMinerDetailsResponse). Normalized to canonical
            lowercase colon form, or None on missing/malformed/all-zeros.

        model:
            /api/v1/miner/details → miner_identity (MinerIdentity) →
            miner_model (str).  Falls back to ``name`` field on the same
            object.  Returns None if miner_identity is absent.
            MinerIdentity schema: {brand: int32, miner_model: str,
                                   name: str, model: int32 (deprecated)}

        boards:
            Always [] — Braiins OS has no per-board API surface comparable
            to ePIC's HBs array at the summary level.

        raw:
            raw_details (GetMinerDetailsResponse body).
        """
        # OpenAPI 1.3.0 — integer status code mapping from
        # GET /api/v1/miner/status description:
        # "unspecified, not_started, normal, paused, suspended, or restricted"
        _STATUS_MAP = {
            0: "unspecified",
            1: "not_started",
            2: "normal",
            3: "paused",
            4: "suspended",
            5: "restricted",
        }
        status_int = raw_details.get("status", 0)
        operating_state = _STATUS_MAP.get(status_int, str(status_int))

        # hashrate_ths: prefer real_hashrate.last_1m → last_5m → last_15m →
        # nominal_hashrate.  All are GigaHashrate {gigahash_per_second: float}.
        hashrate_ths = 0.0
        miner_stats = raw_stats.get("miner_stats") or {}
        if miner_stats:
            real_hr = miner_stats.get("real_hashrate") or {}
            # Try progressive time windows for the most up-to-date reading.
            for window in ("last_1m", "last_5m", "last_15m", "last_1h"):
                window_val = real_hr.get(window) or {}
                ghps = window_val.get("gigahash_per_second")
                if ghps is not None:
                    hashrate_ths = float(ghps) / 1000.0
                    break
            else:
                # Fallback to nominal_hashrate if all real_hashrate windows are null.
                nominal = miner_stats.get("nominal_hashrate") or {}
                ghps = nominal.get("gigahash_per_second")
                if ghps is not None:
                    hashrate_ths = float(ghps) / 1000.0

        # power_w: approximated_consumption.watt
        power_w = 0.0
        power_stats = raw_stats.get("power_stats") or {}
        if power_stats:
            approx = power_stats.get("approximated_consumption") or {}
            watt = approx.get("watt")
            if watt is not None:
                power_w = float(watt)

        # fan_speed: first fan's rpm from cooling/state fans array.
        fan_speed = 0
        fans = raw_cooling.get("fans") or []
        if fans:
            fan_speed = int(fans[0].get("rpm", 0) or 0)

        # hostname: required field in GetMinerDetailsResponse.
        hostname = raw_details.get("hostname") or None

        # model: miner_identity.miner_model (preferred) → miner_identity.name
        model: str | None = None
        miner_identity = raw_details.get("miner_identity") or {}
        if miner_identity:
            model = miner_identity.get("miner_model") or miner_identity.get("name") or None

        # MAC: raw_details['mac_address'] — required field per OpenAPI 1.3.0.
        mac = _extract_canonical_mac(raw_details.get("mac_address"))

        return cls(
            operating_state=operating_state,
            hashrate_ths=hashrate_ths,
            power_w=power_w,
            fan_speed=fan_speed,
            target_voltage_mv=None,
            output_voltage_mv=None,
            hostname=hostname,
            model=model,
            mac=mac,
            boards=[],
            raw=raw_details,
        )

    @classmethod
    def from_luxos(
        cls,
        raw_summary: dict,
        raw_version: dict | None = None,
        raw_stats: dict | None = None,
        raw_config: dict | None = None,
        raw_tunerstatus: dict | None = None,
        raw_fans: dict | None = None,
        raw_power: dict | None = None,
    ) -> MinerSummary:
        """Synthesize a MinerSummary from up to six LuxOS API 3.7 responses.

        LuxOS splits the summary fields across multiple cgminer-style commands:
        - ``summary`` carries hashrate (``GHS av``) and STATUS.
        - ``version`` carries the model string (``VERSION[0]['Type']``).
        - ``tunerstatus`` carries the input-power reading
          (``TUNERSTATUS[0]['Power']``); preferred power source on
          LUXminer 2026.4.3+ where SUMMARY/STATS lack the Power field.
        - ``stats`` is the legacy fallback for power (``STATS[*]['Power']``);
          STATS is a multi-entry array and the Power field appears on only
          one of its entries, so the parser iterates to find it.
        - ``fans`` carries fan speed (``FANS[0]['RPM']`` preferred,
          ``FANS[0]['Speed']`` fallback).
        - ``config`` carries hostname (``CONFIG[0]['Hostname']``) and MAC
          (``CONFIG[0]['MACAddr']``), as confirmed for LUXminer 2026.4.3.
        - ``power`` carries the input-power reading (``POWER[0]['Watts']``);
          highest priority power source on LUXminer 2026.4.3+.

        Aux dicts are optional kwargs; any may be ``None`` when the upstream
        cmd failed transiently. The DTO degrades gracefully: missing
        model / power / fan_speed / hostname become None / 0.0 / 0 / None.

        Hashrate units: LuxOS reports ``GHS av`` (giga-hashes/sec). 1 TH/s =
        1000 GH/s, so the divisor is 1e3 (NOT 1e6 as ePIC's MH/s would use).
        """
        status = raw_summary.get("STATUS", [{}])
        summary = raw_summary.get("SUMMARY", [{}])

        ghs_av = float(summary[0].get("GHS av", 0) or 0) if summary else 0.0
        hashrate_ths = ghs_av / 1e3

        if not status or not summary:
            operating_state = "Offline"
        elif status[0].get("STATUS") == "S":
            operating_state = "Mining" if hashrate_ths > 0 else "Idle"
        else:
            operating_state = "Offline"

        # Power: raw_power cmd highest priority, then tunerstatus, then stats.
        power_w = 0.0
        raw_power_had_watts = False
        if raw_power is not None:
            power_arr = raw_power.get("POWER") or []
            if power_arr and "Watts" in power_arr[0]:
                power_w = float(power_arr[0]["Watts"] or 0)
                raw_power_had_watts = True
        if not raw_power_had_watts:
            tunerstatus_had_power = False
            if raw_tunerstatus is not None:
                tunerstatus_arr = raw_tunerstatus.get("TUNERSTATUS") or []
                if tunerstatus_arr and "Power" in tunerstatus_arr[0]:
                    power_w = float(tunerstatus_arr[0]["Power"] or 0)
                    tunerstatus_had_power = True
            if not tunerstatus_had_power and raw_stats:
                for stat in raw_stats.get("STATS", []) or []:
                    if isinstance(stat, dict) and "Power" in stat:
                        power_w = float(stat["Power"] or 0)
                        break

        model: str | None = None
        if raw_version:
            version = raw_version.get("VERSION", [{}]) or [{}]
            if version:
                model_val = version[0].get("Type")
                model = model_val if model_val else None

        # Fan speed: FANS[0]["RPM"] preferred, "Speed" as fallback.
        fan_speed = 0
        if raw_fans is not None:
            fans_arr = raw_fans.get("FANS") or []
            if fans_arr:
                fan_entry = fans_arr[0]
                if "RPM" in fan_entry:
                    fan_speed = int(fan_entry["RPM"] or 0)
                elif "Speed" in fan_entry:
                    fan_speed = int(fan_entry["Speed"] or 0)

        # Hostname: CONFIG[0]["Hostname"], stripped; empty -> None.
        # MAC: CONFIG[0]["MACAddr"] (verified live: LUXminer 2026.4.3 returns
        # canonical lowercase colon form).
        # Cap hostname at 253 chars (DNS hostname max) to defend against a
        # malicious miner returning a giant string that breaks dashboard layout.
        hostname: str | None = None
        mac: str | None = None
        if raw_config is not None:
            config_arr = raw_config.get("CONFIG") or []
            if isinstance(config_arr, list) and config_arr and isinstance(config_arr[0], dict):
                if "Hostname" in config_arr[0]:
                    raw_hostname = str(config_arr[0]["Hostname"]).strip()[:253]
                    hostname = raw_hostname if raw_hostname else None
                mac = _extract_canonical_mac(config_arr[0].get("MACAddr"))

        return cls(
            operating_state=operating_state,
            hashrate_ths=hashrate_ths,
            power_w=power_w,
            fan_speed=fan_speed,
            target_voltage_mv=None,
            output_voltage_mv=None,
            hostname=hostname,
            model=model,
            mac=mac,
            boards=[],
            raw=raw_summary,
        )


@dataclasses.dataclass
class HardwareTopology:
    num_boards: int
    chips_per_board: int  # 0 means firmware does not expose per-chip data
    psu_min_mv: int
    psu_max_mv: int
    # True only when this adapter obtained and validated both bounds from the
    # connected firmware. Static/spec fallbacks must leave this False.
    psu_bounds_verified: bool = False
    psu_bounds_source: str = "unverified"

    def require_verified_voltage_target(self, target_mv: float | None = None) -> None:
        """Fail closed unless live firmware supplied a sane PSU range.

        ``target_mv`` is optional so Phase 0 can validate provenance before it
        has selected a starting voltage. When supplied, the target must also
        fall inside the verified range.
        """

        source = self.psu_bounds_source or "unspecified"
        if not self.psu_bounds_verified:
            raise UnsafeVoltageBoundsError(
                f"refusing voltage mutation: PSU voltage bounds are unverified (source={source})"
            )

        bounds = (self.psu_min_mv, self.psu_max_mv)
        if any(isinstance(value, bool) for value in bounds):
            raise UnsafeVoltageBoundsError(
                f"refusing voltage mutation: invalid verified PSU bounds {bounds!r}"
            )
        try:
            min_mv, max_mv = (float(value) for value in bounds)
        except (TypeError, ValueError) as exc:
            raise UnsafeVoltageBoundsError(
                f"refusing voltage mutation: invalid verified PSU bounds {bounds!r}"
            ) from exc
        if not (
            math.isfinite(min_mv) and math.isfinite(max_mv) and min_mv >= 1000 and max_mv > min_mv
        ):
            raise UnsafeVoltageBoundsError(
                f"refusing voltage mutation: invalid verified PSU bounds {bounds!r}"
            )

        if target_mv is None:
            return
        if isinstance(target_mv, bool):
            raise UnsafeVoltageBoundsError(
                f"refusing voltage mutation: invalid target {target_mv!r} mV"
            )
        try:
            target = float(target_mv)
        except (TypeError, ValueError) as exc:
            raise UnsafeVoltageBoundsError(
                f"refusing voltage mutation: invalid target {target_mv!r} mV"
            ) from exc
        if not math.isfinite(target) or not min_mv <= target <= max_mv:
            raise UnsafeVoltageBoundsError(
                f"refusing voltage mutation: target {target_mv!r} mV is outside "
                f"verified PSU range [{self.psu_min_mv}, {self.psu_max_mv}] mV "
                f"(source={source})"
            )
