from __future__ import annotations

from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from tuner_app import state
from tuner_app.constants import _PLATFORMS
from tuner_app.miner.registry import supported_firmware_types
from tuner_app.profit.minerstat import get_minerstat_snapshot_copy

from ._mac_helpers import parse_mac_path_segment


def format_log_entry(entry):
    """Render a JSONL log entry as 'HH:MM:SS: msg' for dashboard display."""
    ts = entry.get("ts")
    if isinstance(ts, (int, float)):
        hhmmss = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    else:
        hhmmss = "--:--:--"
    return f"{hhmmss}: {entry.get('msg', '')}"


def status(handler, body) -> None:
    """Handle GET /tuner/status."""
    handler._json_response(handler.manager.get_all_status())


def overview(handler, body) -> None:
    """Handle GET /tuner/overview."""
    handler._json_response(handler.manager.get_overview())


def live(handler, body) -> None:
    """Handle GET /tuner/live/{mac}."""
    raw = handler.path.split("/tuner/live/")[1]
    mac = parse_mac_path_segment(handler, raw, "/tuner/live/")
    if mac is None:
        return
    handler._json_response(handler.manager.get_engine(mac).get_live_data())


def log(handler, body) -> None:
    """Handle GET /tuner/log/{mac}."""
    parts = urlsplit(handler.path)
    raw = parts.path.split("/tuner/log/")[1]
    mac = parse_mac_path_segment(handler, raw, "/tuner/log/")
    if mac is None:
        return
    qs = parse_qs(parts.query)
    voltage_filter = None
    if "voltage_mv" in qs:
        try:
            voltage_filter = int(qs["voltage_mv"][0])
        except (ValueError, IndexError):
            voltage_filter = None
    engine = handler.manager.get_engine(mac)
    # log_lines is a deque of dicts; convert to the legacy line-string
    # format the existing frontend renders. When a voltage filter is
    # provided, return ALL matching entries (no 500-line cap) so the
    # per-step modal is comprehensive.
    if voltage_filter is None:
        entries = list(engine.log_lines)[-500:]
    else:
        entries = [e for e in engine.log_lines if e.get("voltage_mv") == voltage_filter]
    handler._json_response(
        {
            "lines": [format_log_entry(e) for e in entries],
            "entries": entries,
        }
    )


def export(handler, body) -> None:
    """Handle GET /tuner/export/{mac}."""
    from tuner_app.config.defaults import iter_all_config_keys
    from tuner_app.config.effective import EffectiveConfig

    raw = handler.path.split("/tuner/export/")[1]
    mac = parse_mac_path_segment(handler, raw, "/tuner/export/")
    if mac is None:
        return
    # iter_all_config_keys reads state.CONFIG without a lock; hold the lock
    # while we snapshot the key set so it's consistent with the config state.
    # EffectiveConfig.__getitem__ acquires config_lock on every read, so we
    # MUST NOT call it while we hold the lock (non-reentrant deadlock).
    with state.config_lock:
        keys = iter_all_config_keys()
    # Now build the flat snapshot outside the lock — EffectiveConfig handles
    # its own per-read locking.
    ec = EffectiveConfig(mac)
    live_cfg = {k: ec[k] for k in keys}
    handler._json_response(handler.manager.get_engine(mac).get_export(current_config=live_cfg))


def config(handler, body) -> None:
    """Handle GET /tuner/config.

    Response shape (v4 + Phase 1 backward-compat):
        {
          "defaults": {
            "epic":    {...},
            "bixbit":  {...},
            "luxos":   {...},
            "braiins": {...}
          },
          "fleet_ops":    {...},
          "miner_configs": {mac: {...}},
        }

    The "miner_configs" key is keyed by MAC in v4 (the canonical ``MINER_CONFIGS``
    key). v3 callers reading the same response should treat the keys as opaque
    identifiers and look up by MAC; the frontend has been migrated as part of
    the A13 hard cutover.
    """
    with state.config_lock:
        handler._json_response(
            {
                "defaults": {p: dict(state.CONFIG["defaults"][p]) for p in _PLATFORMS},
                "fleet_ops": dict(state.CONFIG["fleet_ops"]),
                "miner_configs": {mac: dict(ov) for mac, ov in state.MINER_CONFIGS.items()},
            }
        )


def minerstat_snapshot(handler, body) -> None:
    """Handle GET /tuner/minerstat/snapshot."""
    # Returns the current minerstat snapshot (may be empty if never
    # fetched). Dashboard card polls this alongside /tuner/overview.
    snap = get_minerstat_snapshot_copy()
    handler._json_response(
        {
            "snapshot": snap,
            "poll_day": state.CONFIG["fleet_ops"].get("MINERSTAT_POLL_DAY", 0),
            "income_modifier_pct": state.CONFIG["fleet_ops"].get("INCOME_MODIFIER_PCT", 0.0),
        }
    )


def firmware_types(handler, body) -> None:
    """Handle GET /tuner/firmware_types."""
    handler._json_response({"firmware_types": supported_firmware_types()})
