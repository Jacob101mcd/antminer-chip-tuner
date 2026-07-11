"""Scoring contexts for ranking and display.

Ranking-vs-display split: `get_scoring_context` returns coin_data=None in
efficiency mode (so ranking stays on J/TH) while `get_profit_display_context`
returns coin_data whenever a minerstat snapshot is available (so dashboard
can show $/day independent of TARGET_MODE).
"""

from __future__ import annotations

from tuner_app.profit.compute import score_cell
from tuner_app.profit.minerstat import get_minerstat_snapshot_copy


def get_scoring_context(engine):
    """Bundle (target_mode, electric_rate, coin_data, income_modifier_pct)
    for score_cell.

    Reads engine.config (which respects per-miner overrides via EffectiveConfig)
    plus the module-level minerstat snapshot. If profit mode is requested but
    no coin data is available, silently falls back to efficiency so every
    decision point stays well-defined — the dashboard surfaces the missing-
    snapshot state separately via the minerstat card.

    Returns a tuple suitable for `score_cell(entry, *ctx)`.
    """
    mode = engine.config.get("TARGET_MODE", "efficiency") or "efficiency"
    if mode != "profitability":
        return ("efficiency", 0.0, None, 0.0)
    rate, coin_data, modifier = get_profit_display_context(engine)
    if coin_data is None:
        return ("efficiency", 0.0, None, 0.0)
    return ("profitability", rate, coin_data, modifier)


def get_profit_display_context(engine):
    """Fetches (electric_rate, coin_data, income_modifier_pct) for
    profit-display purposes, independent of TARGET_MODE. The scoring
    context refuses to return coin_data in efficiency mode so that ranking
    stays on J/TH, but the dashboard still wants to show $/day alongside
    J/TH so operators can see the profit crossover regardless of which
    metric they're tuning to. Returns (rate, None, modifier) when no
    minerstat snapshot or configured coin is available — callers must
    handle coin_data=None."""
    try:
        rate = float(engine.config.get("ELECTRIC_RATE_PER_KWH", 0.10) or 0.10)
    except (TypeError, ValueError):
        rate = 0.10
    try:
        modifier = float(engine.config.get("INCOME_MODIFIER_PCT", 0.0) or 0.0)
    except (TypeError, ValueError):
        modifier = 0.0
    snapshot = get_minerstat_snapshot_copy()
    coin_id = (engine.config.get("MINERSTAT_COIN", "BTC") or "BTC").strip().upper()
    coins = snapshot.get("coins", {}) if snapshot else {}
    return (rate, coins.get(coin_id), modifier)


def score_key(engine, ctx=None):
    """Return a min()/sorted() key callable. Unscorable entries map to
    float('inf') so they sink to the bottom of any ranking — same contract
    as the pre-refactor `key=lambda r: r.get("efficiency_jth", float("inf"))`
    pattern, just extended to support profit mode."""
    if ctx is None:
        ctx = get_scoring_context(engine)

    def key(entry):
        s = score_cell(entry, *ctx)
        return float("inf") if s is None else s

    return key
