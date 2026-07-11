from __future__ import annotations

import json
import logging

from tuner_app import state
from tuner_app.config.effective import EffectiveConfig, resolve_current_firmware
from tuner_app.config.persistence import save_config_to_disk
from tuner_app.config.validation import validate_config
from tuner_app.constants import _PLATFORMS, RESET_SCOPES
from tuner_app.manager.bulk import _bulk_run, _delete_profile_for_ip, _make_remove_action
from tuner_app.net.http_client import miner_http_request

from ._mac_helpers import parse_macs_body_field

logger = logging.getLogger(__name__)


def _make_start_action(manager):
    """Return a per-MAC closure that calls manager.start_tuning."""

    def action(mac):
        return {"started": bool(manager.start_tuning(mac))}

    return action


def _make_stop_action(manager):
    """Return a per-MAC closure that calls manager.stop_tuning."""

    def action(mac):
        manager.stop_tuning(mac)
        return {"stopped": True}

    return action


def _make_start_mining_action(manager):
    """Return a per-MAC closure that calls api.start_mining (raw vendor cmd).

    Distinct from start_tuning: this resumes hashing on the miner itself
    (e.g. ePIC Resume, Bixbit power_on) without touching the tuner thread.
    """

    def action(mac):
        manager.get_engine(mac).api.start_mining()
        return {"start_mining": True}

    return action


def _make_stop_mining_action(manager):
    """Return a per-MAC closure that calls api.stop_mining (raw vendor cmd)."""

    def action(mac):
        manager.get_engine(mac).api.stop_mining()
        return {"stop_mining": True}

    return action


def _make_reboot_action(manager):
    """Return a per-MAC closure that reboots the miner (delay=0)."""

    def action(mac):
        manager.get_engine(mac).api.reboot(delay=0)
        return {"reboot": True}

    return action


def _make_set_power_limit_action(manager, watts: int):
    """Return a per-MAC closure that sets a fleet-wide power limit.

    Capability-gated: miners whose api reports
    ``has_external_power_limit() == False`` (ePIC) report
    ``capability_unsupported`` and count as failures in the bulk summary,
    mirroring the platform-mismatch pattern.
    """

    def action(mac):
        api = manager.get_engine(mac).api
        if not api.has_external_power_limit():
            raise RuntimeError("capability_unsupported: vendor has no external power limit")
        api.set_power_limit(watts)
        return {"set_power_limit": True, "watts": watts}

    return action


def _intent_for_engine_phase(engine) -> str:
    """Map engine.phase → MRR sync intent. Mirrors mrr_routes.resync."""
    phase = getattr(engine, "phase", "") or ""
    cls = type(engine)
    perpetual = getattr(cls, "PHASE_PERPETUAL", None)
    stopped = getattr(cls, "PHASE_STOPPED", None)
    idle = getattr(cls, "PHASE_IDLE", None)
    error = getattr(cls, "PHASE_ERROR", None)
    if phase == perpetual:
        return "maintaining"
    if phase in (stopped, idle):
        return "stopped"
    if phase == error:
        return "error"
    return "tuning"


def _make_mrr_resync_action(manager):
    """Return a per-MAC closure that re-fires the engine's MRR sync.

    Intent is derived from the engine's current phase using the same
    mapping as mrr_routes.resync, so a fleet-wide bulk action behaves
    identically to flipping each miner's resync button by hand.
    """

    def action(mac):
        engine = manager.get_engine(mac)
        intent = _intent_for_engine_phase(engine)
        engine._mrr_sync(intent, reason="Bulk resync")
        return {"mrr_resync": True, "intent": intent, "last_sync": engine.mrr_last_sync}

    return action


def _make_retune_voltage_action(manager):
    """Return a per-MAC closure that retunes at the engine's currently-active voltage.

    No operator-supplied voltage in the bulk path — that would be a
    fleet-wide cross-platform value, which is rarely the right thing.
    Per-miner explicit voltage retunes still go through the single-miner
    /tuner/retune_voltage endpoint.
    """

    def action(mac):
        engine = manager.get_engine(mac)
        voltage = getattr(engine, "active_sweep_voltage_mv", None)
        if voltage is None:
            raise RuntimeError("no active voltage — miner has no profile to retune")
        ok, err = manager.retune_voltage(mac, int(voltage))
        if not ok:
            raise RuntimeError(err or "retune refused")
        return {"retune_voltage": True, "voltage_mv": int(voltage)}

    return action


def _make_reset_profile_action(scope):
    """Return a per-MAC closure that resets the miner's profile at the given scope."""

    def action(mac):
        _delete_profile_for_ip(mac, scope=scope)
        return {"reset": True, "scope": scope}

    return action


def _bulk_run_platform_aware(macs, cleaned_config, platform):
    """Run per-MAC config apply with platform-match filtering.

    Unlike _bulk_run, this function handles the per-MAC result shape directly
    so that a platform mismatch surfaces as ``{ok: False, reason:
    "platform_mismatch", ...}`` rather than a raised exception.  _bulk_run
    cannot express this distinction because it unconditionally marks successful
    action returns as ``ok: True``.

    *macs* must be canonical colon-form MACs (caller validated via
    parse_macs_body_field). The result map is keyed by MAC for the wire
    response so the frontend can correlate with its `currentMiner` map.
    """
    results = {}
    succeeded = 0
    failed = 0
    seen: set = set()
    for mac in macs:
        if not isinstance(mac, str) or not mac.strip():
            continue
        mac = mac.strip()
        if mac in seen:
            continue
        seen.add(mac)
        try:
            with state.config_lock:
                ov = state.MINER_CONFIGS.get(mac, {})
                actual_platform = resolve_current_firmware(ov)
                if actual_platform != platform:
                    results[mac] = {
                        "ok": False,
                        "error": None,
                        "detail": {
                            "reason": "platform_mismatch",
                            "expected": platform,
                            "actual": actual_platform,
                        },
                    }
                    # Treat as a non-error skip for the summary counter.
                    # Still surfaces as ok=False so the UI can highlight it.
                    failed += 1
                    continue
                ov_existing = state.MINER_CONFIGS.setdefault(mac, {})
                # v4 shape: per-platform overrides nest under ``platforms[<fw>]``;
                # fleet-ops + cross-platform keys live at the top level. The
                # bulk-apply endpoint validates against ``platform`` per-platform
                # tuning keys, so all writes go to the platform bucket.
                platforms_map = ov_existing.setdefault("platforms", {})
                fw_bucket = platforms_map.setdefault(platform, {})
                for k, v in cleaned_config.items():
                    fw_bucket[k] = v
                save_config_to_disk()
            results[mac] = {
                "ok": True,
                "error": None,
                "detail": {"keys": list(cleaned_config.keys())},
            }
            succeeded += 1
        except Exception as ex:
            results[mac] = {"ok": False, "error": f"{type(ex).__name__}: {ex}", "detail": None}
            failed += 1
    return {
        "results": results,
        "summary": {"total": len(results), "succeeded": succeeded, "failed": failed},
    }


def _make_set_pools_action(coin, stratum_configs, manager):
    """Return a per-MAC closure that POSTs /coin to that miner's ePIC API.

    Calls the miner client directly because the action targets a specific
    miner per call. The IP is resolved from the engine's effective config
    (so DHCP moves picked up by the scanner are honored).  Password also
    comes from the effective config so per-miner PASSWORD overrides apply.
    """

    def action(mac):
        cfg = EffectiveConfig(mac)
        ip = cfg.ip
        if not ip:
            raise RuntimeError("no IP recorded for this miner — scanner has not yet seen it")
        password = cfg.get("PASSWORD", "letmein")
        port = state.CONFIG["fleet_ops"]["API_PORT"]
        payload = json.dumps(
            {
                "password": password,
                "param": {"coin": coin, "stratum_configs": stratum_configs},
            }
        ).encode("utf-8")
        status, _headers, resp_body = miner_http_request(
            ip, port, "/coin", data=payload, method="POST", timeout=15
        )
        if status < 200 or status >= 300:
            raise RuntimeError(f"miner pool update returned HTTP {status}")
        try:
            parsed = json.loads(resp_body)
        except ValueError:
            raise RuntimeError("miner pool update returned invalid JSON") from None
        if parsed.get("result") is False:
            raise RuntimeError("miner rejected pool config")
        return {"accepted": True}

    return action


def bulk_start(handler, body) -> None:
    """Handle POST /tuner/bulk/start."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_start_action(handler.manager)))


def bulk_stop(handler, body) -> None:
    """Handle POST /tuner/bulk/stop."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_stop_action(handler.manager)))


def bulk_reset_profile(handler, body) -> None:
    """Handle POST /tuner/bulk/reset_profile."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    scope = (data.get("scope") or "all").strip()
    if scope not in RESET_SCOPES:
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": [f"invalid scope: {scope}"],
            },
            status=400,
        )
    else:
        handler._json_response(_bulk_run(macs, _make_reset_profile_action(scope)))


def bulk_apply_config(handler, body) -> None:
    """Handle POST /tuner/bulk/apply_config.

    Body shape: {"macs": [...], "platform": "epic|bixbit|luxos|braiins", "config": {...}}

    Validates against ``platform``. Per-MAC: if the miner's current_firmware
    does not match ``platform``, the result entry is
    ``{ok: false, reason: "platform_mismatch", expected: platform, actual: <fw>}``
    instead of an error. Otherwise validates per-platform and applies.
    Missing or null "platform" returns HTTP 400.
    """
    data = json.loads(body) if body else {}
    cfg = data.get("config") or {}
    platform = data.get("platform")

    if not isinstance(cfg, dict):
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": ["config must be an object"],
            },
            status=400,
        )
        return

    if platform is None:
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": [f"'platform' is required — must be one of {list(_PLATFORMS)}"],
            },
            status=400,
        )
        return

    if platform not in _PLATFORMS:
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": [f"platform must be one of {list(_PLATFORMS)} (got {platform!r})"],
            },
            status=400,
        )
        return

    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return

    cleaned, errors = validate_config(cfg, platform=platform)
    if errors:
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": errors,
            },
            status=400,
        )
        return
    handler._json_response(_bulk_run_platform_aware(macs, cleaned, platform))


def bulk_remove(handler, body) -> None:
    """Handle POST /tuner/bulk/remove.

    Same per-MAC atomic remove as /tuner/miners/remove (config drop,
    engine destroy + join, full per-miner file wipe including
    .log.jsonl). Per-MAC errors surface in the standard bulk-result
    shape; one bad MAC doesn't abort the batch."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_remove_action(handler.manager)))


def bulk_start_mining(handler, body) -> None:
    """Handle POST /tuner/bulk/start_mining for supported firmware adapters."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_start_mining_action(handler.manager)))


def bulk_stop_mining(handler, body) -> None:
    """Handle POST /tuner/bulk/stop_mining for supported firmware adapters."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_stop_mining_action(handler.manager)))


def bulk_reboot(handler, body) -> None:
    """Handle POST /tuner/bulk/reboot. delay=0 always; rolling-reboot is
    a separate concern not exposed today."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_reboot_action(handler.manager)))


def bulk_set_power_limit(handler, body) -> None:
    """Handle POST /tuner/bulk/set_power_limit.

    Body: {"macs": [...], "watts": <int>}. Capability-gated — miners
    whose api reports has_external_power_limit() is False (ePIC) report
    ``capability_unsupported`` per-MAC and count as failed in the summary.
    """
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    raw_watts = data.get("watts")
    try:
        watts = int(raw_watts)
    except (TypeError, ValueError):
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": ["'watts' must be an integer"],
            },
            status=400,
        )
        return
    if not (500 <= watts <= 10000):
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": [f"'watts' must be in [500, 10000] (got {watts})"],
            },
            status=400,
        )
        return
    handler._json_response(_bulk_run(macs, _make_set_power_limit_action(handler.manager, watts)))


def bulk_mrr_resync(handler, body) -> None:
    """Handle POST /tuner/bulk/mrr_resync. Per-MAC intent derived from each engine's phase."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_mrr_resync_action(handler.manager)))


def bulk_retune_voltage(handler, body) -> None:
    """Handle POST /tuner/bulk/retune_voltage. Each engine's currently-active voltage."""
    data = json.loads(body) if body else {}
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(_bulk_run(macs, _make_retune_voltage_action(handler.manager)))


def bulk_pools(handler, body) -> None:
    """Handle POST /tuner/bulk/pools."""
    data = json.loads(body) if body else {}
    stratums = data.get("stratum_configs") or []
    coin = data.get("coin") or "BTC"
    stratums_clean = [s for s in stratums if isinstance(s, dict) and (s.get("pool") or "").strip()]
    if not stratums_clean:
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": ["at least one stratum_config with a non-empty pool is required"],
            },
            status=400,
        )
        return
    macs = parse_macs_body_field(handler, data)
    if macs is None:
        return
    handler._json_response(
        _bulk_run(macs, _make_set_pools_action(coin, stratums_clean, handler.manager))
    )
