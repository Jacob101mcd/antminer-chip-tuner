"""
Pure profitability scoring functions for tuner application.

Every decision point (Phase V winner selection, 8-ray walk, top-K, refinement
winner, _refresh_sweep_reference, _compute_top_tunes) used to hardcode
`key=lambda r: r["efficiency_jth"]` with an implicit "lower is better"
convention. `score_cell` centralizes that convention: it always returns a
scalar where LOWER IS BETTER, regardless of mode (profit is negated). That
lets every `min(...)` / `sorted(...)` call stay shape-compatible with a pure
refactor — no flipping > vs <, no branching per call site.

Returning None means "don't rank this cell": either the measurement never
landed (API failure, chip crash), or we're in profit mode but have no
minerstat snapshot loaded yet. Callers must filter None before ranking.
"""

from __future__ import annotations


def compute_profit_usd_per_day(
    hashrate_ths: float | None,
    power_w: float | None,
    coin_data: dict | None,
    electric_rate: float | None,
    income_modifier_pct: float = 0.0,
) -> float | None:
    """Given a measured (hashrate, power) point and current coin market data,
    return profit $/day = revenue - electric cost.

    Args:
        hashrate_ths: measured per-miner hashrate in TH/s (e.g. 201.4)
        power_w: measured total input power in watts (e.g. 4120)
        coin_data: dict with keys price_usd, reward_block, network_hashrate (H/s),
                   block_time_s. network_hashrate is total network H/s (e.g. 5.2e20
                   for BTC). block_time_s is the target inter-block time (e.g. 600
                   for BTC).
        electric_rate: $/kWh paid for power (e.g. 0.08)
        income_modifier_pct: revenue-side percentage adjustment (e.g. +9.5 when
                   rigs are rented out via MiningRigRentals at a premium over
                   raw pool revenue, -5 when pool fees depress realized income
                   below the raw math). 0.0 = no modifier. Applied only to
                   revenue; cost is not affected.

    Returns:
        Profit in USD per day (can be negative when power cost exceeds revenue).
        Returns None if any input is missing or the math would divide by zero.
    """
    if hashrate_ths is None or power_w is None or coin_data is None:
        return None
    if electric_rate is None:
        return None
    try:
        price_usd = float(coin_data["price_usd"])
        reward_block = float(coin_data["reward_block"])
        network_hs = float(coin_data["network_hashrate"])
        block_time_s = float(coin_data["block_time_s"])
    except (KeyError, TypeError, ValueError):
        return None
    if network_hs <= 0 or block_time_s <= 0:
        return None
    try:
        modifier = float(income_modifier_pct or 0.0)
    except (TypeError, ValueError):
        modifier = 0.0
    # Coin mined per TH/s per day:
    #   blocks_per_day = 86400 / block_time_s
    #   our_fraction_of_network = (hashrate_ths * 1e12) / network_hashrate_hs
    #   coin_per_day = blocks_per_day * reward_block * our_fraction
    # Rearranged per 1 TH/s for readability:
    coin_per_th_day = (86400.0 / block_time_s) * reward_block * (1e12 / network_hs)
    revenue_usd_day = float(hashrate_ths) * coin_per_th_day * price_usd * (1.0 + modifier / 100.0)
    cost_usd_day = (float(power_w) * 24.0 / 1000.0) * float(electric_rate)
    return revenue_usd_day - cost_usd_day


def score_cell(
    entry: dict | None,
    target_mode: str,
    electric_rate: float,
    coin_data: dict | None,
    income_modifier_pct: float = 0.0,
) -> float | None:
    """Return a scalar where LOWER is better, regardless of mode. Used by
    every min()/sorted() ranking in the engine so efficiency vs profitability
    is a pure plug-in swap at every decision point.

    - "efficiency" mode: returns entry["efficiency_jth"] directly.
    - "profitability" mode: returns -compute_profit_usd_per_day(...) so min()
      still picks the best cell (smallest negative profit = largest profit).

    `income_modifier_pct` is a revenue-side percentage adjustment passed
    through to compute_profit_usd_per_day (profit mode only; ignored in
    efficiency mode since ranking stays on J/TH there).

    Returns None when the cell can't be scored: missing measurement data,
    profit mode with no coin snapshot, or the cell is thermal_failed (the
    measurement attempt was aborted because chips overheated; we don't want
    these cells participating in ranking, fine-grid selection, chip-tune
    selection, or trend-confirm). Callers must filter None before passing
    to min()/sorted() since Python 3 can't order None vs float.
    """
    if entry is None:
        return None
    if entry.get("thermal_failed"):
        return None
    if target_mode == "profitability":
        if coin_data is None:
            return None
        profit = compute_profit_usd_per_day(
            entry.get("hashrate_ths"),
            entry.get("power_w"),
            coin_data,
            electric_rate,
            income_modifier_pct,
        )
        if profit is None:
            return None
        return -profit
    # Efficiency mode (and any unknown mode for safety) — fall back to J/TH.
    jth = entry.get("efficiency_jth")
    if jth is None:
        return None
    return float(jth)
