from __future__ import annotations

import json

from tuner_app import state
from tuner_app.miner.exceptions import MRRError
from tuner_app.mrr.client import MRRClient
from tuner_app.mrr.rental_cache import rental_cache


def whoami(handler, body) -> None:
    """Handle GET /tuner/mrr/whoami."""
    # Verify MRR credentials + return user info. Used by the Test
    # Connection button on the MRR settings modal.
    with state.config_lock:
        api_key = state.CONFIG["fleet_ops"].get("MRR_API_KEY", "") or ""
        api_secret = state.CONFIG["fleet_ops"].get("MRR_API_SECRET", "") or ""
    if not api_key or not api_secret:
        handler._json_response(
            {
                "ok": False,
                "error": "MRR credentials not configured",
            },
            status=400,
        )
        return
    try:
        data = MRRClient(api_key, api_secret).whoami()
        handler._json_response({"ok": True, "error": "", "data": data})
    except MRRError as ex:
        handler._json_response(
            {
                "ok": False,
                "error": str(ex),
            },
            status=502,
        )


def rigs(handler, body) -> None:
    """Handle GET /tuner/mrr/rigs."""
    # List owned rigs for the per-miner rig-ID picker. Returns the
    # raw MRR array shape so the frontend can extract id/name/type/
    # status/hash without a schema translation layer.
    with state.config_lock:
        api_key = state.CONFIG["fleet_ops"].get("MRR_API_KEY", "") or ""
        api_secret = state.CONFIG["fleet_ops"].get("MRR_API_SECRET", "") or ""
    if not api_key or not api_secret:
        handler._json_response(
            {
                "ok": False,
                "error": "MRR credentials not configured",
                "rigs": [],
            },
            status=400,
        )
        return
    try:
        rigs_list = MRRClient(api_key, api_secret).list_my_rigs()
        handler._json_response({"ok": True, "error": "", "rigs": rigs_list})
    except MRRError as ex:
        handler._json_response(
            {
                "ok": False,
                "error": str(ex),
                "rigs": [],
            },
            status=502,
        )


def rental_status(handler, body) -> None:
    """Handle GET /tuner/mrr/rental_status."""
    cache_data = rental_cache.get_all()
    handler._json_response({"ok": True, "rental_status": cache_data})


def resync(handler, body) -> None:
    """Handle POST /tuner/mrr/resync."""
    # Manual resync — operator-triggered from the dashboard. Fires
    # _mrr_sync with the intent matching the engine's CURRENT phase
    # so whatever the tuner believes is "right now" gets pushed to
    # MRR. Useful after editing the modifier % or when the last
    # auto-sync failed.
    from tuner_app.http_server.handlers._mac_helpers import parse_mac_body_field

    data = json.loads(body) if body else {}
    mac = parse_mac_body_field(handler, data)
    if mac is None:
        return
    try:
        engine = handler.manager.get_engine(mac)
    except Exception as ex:
        handler._json_response(
            {
                "ok": False,
                "error": f"engine init failed: {ex}",
            }
        )
        return
    # Map current phase → sync intent. Phase 6 is "maintaining"
    # regardless of whether we already announced; operator
    # explicitly clicking resync is a force-push.
    phase = getattr(engine, "phase", "") or ""
    engine_cls = type(engine)
    phase_perpetual = getattr(engine_cls, "PHASE_PERPETUAL", None)
    phase_stopped = getattr(engine_cls, "PHASE_STOPPED", None)
    phase_idle = getattr(engine_cls, "PHASE_IDLE", None)
    phase_error = getattr(engine_cls, "PHASE_ERROR", None)
    if phase == phase_perpetual:
        intent = "maintaining"
    elif phase in (phase_stopped, phase_idle):
        intent = "stopped"
    elif phase == phase_error:
        intent = "error"
    else:
        # Any tuning phase (discovery, baseline, Phase V, Phase 3,
        # Phase 3b, Phase 4, Phase 5) or offline — rig shouldn't
        # be advertising right now.
        intent = "tuning"
    engine._mrr_sync(intent, reason="Manual resync")
    handler._json_response(
        {
            "ok": True,
            "error": "",
            "last_sync": engine.mrr_last_sync,
        }
    )
