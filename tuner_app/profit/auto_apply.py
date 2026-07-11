"""Auto-apply profit recompute helper.

Called whenever a fresh minerstat snapshot arrives — both the manual
"Fetch now" HTTP handler and the MinerstatScheduler daemon's tick funnel
through here. Replaces the operator-confirmed Recompute & Apply modal
that previously sat in front of the same logic.
"""

from __future__ import annotations

import logging
from typing import Any

from tuner_app import state

logger = logging.getLogger(__name__)


def apply_profit_recompute(manager, ips: list[str] | None = None) -> dict[str, Any]:
    """Recompute per-miner profit winners and apply them.

    If ``ips`` is None, defaults to the fleet IP list under ``MINER_IPS``.
    Returns ``{applied, skipped, failures}`` where ``failures`` is a list of
    ``"<ip>: <msg>"`` strings suitable for inclusion in HTTP responses or
    operator log lines.

    Locking: ``compute_profit_preview`` already acquires
    ``state.minerstat_lock`` for snapshot reads; ``apply_profit_action``
    acquires per-engine locks. This helper acquires only ``state.config_lock``
    briefly to snapshot the fleet IP list when ``ips`` is None.
    """
    if ips is None:
        with state.config_lock:
            ips = list(state.CONFIG["fleet_ops"].get("MINER_IPS", []))
    if not ips:
        return {"applied": 0, "skipped": 0, "failures": []}
    try:
        preview = manager.compute_profit_preview(ips)
    except Exception as e:
        logger.exception("[auto-apply] preview failed: %s", e)
        return {"applied": 0, "skipped": 0, "failures": [f"preview: {e}"]}
    applied = 0
    skipped = 0
    failures: list[str] = []
    for miner in preview.get("miners", []):
        ip = miner.get("ip")
        if not ip or "proposed" not in miner:
            skipped += 1
            continue
        proposed = miner["proposed"]
        action = proposed.get("action", "none")
        if action == "none":
            skipped += 1
            continue
        try:
            ok, err, _detail = manager.apply_profit_action(
                ip, action, int(proposed["voltage_mv"]), proposed.get("freq_mhz")
            )
            if ok:
                applied += 1
                logger.info(
                    "[auto-apply] %s %s @ %s mV → ok",
                    ip,
                    action,
                    proposed["voltage_mv"],
                )
            else:
                failures.append(f"{ip}: {err}")
                logger.warning(
                    "[auto-apply] %s %s @ %s mV → FAIL: %s",
                    ip,
                    action,
                    proposed["voltage_mv"],
                    err,
                )
        except Exception as e:
            failures.append(f"{ip}: {e}")
            logger.exception("[auto-apply] %s action errored: %s", ip, e)
    return {"applied": applied, "skipped": skipped, "failures": failures}
