from __future__ import annotations

import json
import os
from datetime import datetime

from tuner_app.config.effective import resolve_current_firmware
from tuner_app.config.persistence import _atomic_json_write
from tuner_app.constants import RESET_SCOPES, _miner_data_path, _miner_platform_path
from tuner_app.manager.bulk import _delete_profile_for_ip

from ._mac_helpers import parse_mac_body_field


def _stock_path_for_engine(handler, mac):
    """Resolve the per-platform .stock.json path for *mac*.

    Reads the engine's firmware_type via peek_engine when available; falls
    back to ``MINER_CONFIGS[mac]["current_firmware"]`` (default ``"epic"``)
    when no engine has been spawned yet. Returns the per-platform path so a
    reflashed miner's stock baseline doesn't leak across firmware variants.
    """
    from tuner_app import state

    engine = handler.manager.peek_engine(mac)
    if engine is not None:
        firmware_type = engine.firmware_type
    else:
        with state.config_lock:
            firmware_type = resolve_current_firmware(state.MINER_CONFIGS.get(mac, {}))
    try:
        return _miner_platform_path(mac, firmware_type, ".stock.json")
    except (TypeError, ValueError):
        # Legacy fallback for tests that inject MAC-shaped synthetic IDs
        return _miner_data_path(mac, ".stock.json")


def start(handler, body) -> None:
    """Handle POST /tuner/start."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data, response_key="started")
    if mac is None:
        return
    handler._json_response({"started": handler.manager.start_tuning(mac)})


def stop(handler, body) -> None:
    """Handle POST /tuner/stop."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data, response_key="stopped")
    if mac is None:
        return
    handler.manager.stop_tuning(mac)
    handler._json_response({"stopped": True})


def delete_profile(handler, body) -> None:
    """Handle POST /tuner/delete_profile."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data, response_key="deleted")
    if mac is None:
        return
    scope = (data.get("scope") or "all").strip()
    if scope not in RESET_SCOPES:
        handler._json_response({"deleted": False, "error": f"invalid scope: {scope}"}, status=400)
        return
    _delete_profile_for_ip(mac, scope=scope)
    handler._json_response({"deleted": True, "scope": scope})


def reset_stock(handler, body) -> None:
    """Handle POST /tuner/reset_stock."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    manual = data.get("baseline")
    if manual is not None:
        # Manual baseline: operator typed the stock numbers in directly
        # (hashboard swap where the original capture is gone, or they
        # want to override what the live sample produced). Persist with
        # source="manual" so it survives across Reset Profile and is
        # preserved in _capture_live_stock_baseline's skip logic.
        try:
            ths = float(manual.get("hashrate_ths"))
            power = float(manual.get("power_w"))
            volt = float(manual.get("voltage_mv"))
        except (TypeError, ValueError):
            handler._json_response(
                {
                    "ok": False,
                    "error": "hashrate_ths / power_w / voltage_mv must be numbers",
                }
            )
            return
        if not (0 < ths < 1000 and 0 < power < 20000 and 5000 < volt < 20000):
            handler._json_response({"ok": False, "error": "values out of plausible range"})
            return
        baseline = {
            "hashrate_ths": ths,
            "power_w": power,
            "efficiency_jth": power / ths,
            "voltage_mv": volt,
            "source": "manual",
            "captured_at": datetime.now().isoformat(),
        }
        filepath = _stock_path_for_engine(handler, mac)
        _atomic_json_write(filepath, baseline)
        engine = handler.manager.peek_engine(mac)
        if engine:
            engine.stock_baseline = baseline
        handler._json_response({"ok": True, "baseline": baseline})
    else:
        # Delete the file and zero the in-memory value on the live
        # engine so the next Phase 0 re-captures against the miner.
        filepath = _stock_path_for_engine(handler, mac)
        if os.path.exists(filepath):
            os.remove(filepath)
        engine = handler.manager.peek_engine(mac)
        if engine:
            engine.stock_baseline = dict(engine.STOCK_SPEC)
        handler._json_response({"ok": True})


def retune_voltage(handler, body) -> None:
    """Handle POST /tuner/retune_voltage."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    voltage_mv = data.get("voltage_mv")
    if not isinstance(voltage_mv, (int, float)):
        handler._json_response({"ok": False, "error": "mac and voltage_mv required"})
        return
    ok, err = handler.manager.retune_voltage(mac, int(voltage_mv))
    handler._json_response({"ok": ok, "error": err})


def select_voltage_profile(handler, body) -> None:
    """Handle POST /tuner/select_voltage_profile."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    voltage_mv = data.get("voltage_mv")
    if not isinstance(voltage_mv, (int, float)):
        handler._json_response({"ok": False, "error": "mac and voltage_mv required"})
        return
    try:
        handler.manager.select_voltage_profile(mac, int(voltage_mv))
        handler._json_response({"ok": True, "error": ""})
    except ValueError as ex:
        handler._json_response({"ok": False, "error": str(ex)})
    except Exception as ex:
        handler._json_response({"ok": False, "error": f"apply failed: {ex}"})


def remeasure_cell(handler, body) -> None:
    """Handle POST /tuner/remeasure_cell."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    voltage_mv = data.get("voltage_mv")
    freq_mhz = data.get("freq_mhz")
    if not isinstance(voltage_mv, (int, float)) or not isinstance(freq_mhz, (int, float)):
        handler._json_response({"ok": False, "error": "mac, voltage_mv, and freq_mhz required"})
        return
    try:
        added, size = handler.manager.enqueue_remeasure(mac, int(voltage_mv), float(freq_mhz))
        handler._json_response(
            {
                "ok": True,
                "error": "",
                "added": added,
                "queue_size": size,
            }
        )
    except Exception as ex:
        handler._json_response({"ok": False, "error": str(ex)})


def remeasure_queue_clear(handler, body) -> None:
    """Handle POST /tuner/remeasure_queue/clear."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    try:
        handler.manager.clear_remeasure_queue(mac)
        handler._json_response({"ok": True, "error": ""})
    except Exception as ex:
        handler._json_response({"ok": False, "error": str(ex)})


def remeasure_queue_process(handler, body) -> None:
    """Handle POST /tuner/remeasure_queue/process."""
    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    ok, err = handler.manager.start_remeasure_queue(mac)
    handler._json_response({"ok": ok, "error": err})
