from __future__ import annotations

from tuner_app import state
from tuner_app.profit.auto_apply import apply_profit_recompute
from tuner_app.profit.minerstat import (
    MinerstatError,
    fetch_minerstat_coins,
    save_minerstat_snapshot,
)


def fetch_now(handler, body) -> None:
    """Handle POST /tuner/minerstat/fetch_now.

    Manual "Fetch now" trigger. Burns 1 minerstat API call. After the
    snapshot is persisted, the shared ``apply_profit_recompute`` helper
    fans out per-miner actions (same path the scheduler uses), and the
    response body carries an ``auto_apply`` summary so the frontend can
    show a brief toast about what was applied.
    """
    with state.config_lock:
        coin = (state.CONFIG["fleet_ops"].get("MINERSTAT_COIN", "BTC") or "BTC").upper()
        api_key = state.CONFIG["fleet_ops"].get("MINERSTAT_API_KEY", "") or ""
    coins = {coin} if coin else set()
    if not coins:
        handler._json_response({"ok": False, "error": "no coins configured"}, status=400)
        return
    try:
        result = fetch_minerstat_coins(sorted(coins), api_key=api_key)
        payload = save_minerstat_snapshot(result, api_calls_increment=1)
    except MinerstatError as ex:
        handler._json_response(
            {
                "ok": False,
                "error": f"minerstat fetch failed: {ex}",
            },
            status=502,
        )
        return
    except Exception as ex:
        handler._json_response(
            {
                "ok": False,
                "error": f"unexpected error: {ex}",
            },
            status=500,
        )
        return
    auto_apply = apply_profit_recompute(handler.manager)
    handler._json_response(
        {
            "ok": True,
            "error": "",
            "snapshot": payload,
            "auto_apply": auto_apply,
        }
    )
