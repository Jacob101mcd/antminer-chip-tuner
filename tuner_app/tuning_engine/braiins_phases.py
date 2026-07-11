"""Wattage binary-search tuning loop for Braiins OS miners.

BOS firmware owns its own internal V/F optimizer (AutoTune / upfreq); we cannot
do per-chip clock work.  This module implements a 1-D binary search over a
power-target wattage range, letting BOS settle at each point, measuring
efficiency / profit, and converging on the best watt target.  After convergence
the loop switches to periodic perpetual monitoring at the chosen wattage.

Public entry point:
    run_braiins_loop(engine) -> None
        Called from TuningEngine._run_inner after _phase0_discovery when
        firmware_type == "braiins".  Returns only when engine.running flips
        False (operator Stop).
"""

from __future__ import annotations

import math
import time

from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.profit.compute import compute_profit_usd_per_day
from tuner_app.tuning_engine.scoring import get_profit_display_context

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _init_search_bounds(engine) -> None:
    """Clamp / initialize wattage_search_low and wattage_search_high from
    CONFIG knobs.  Called at loop entry and again whenever the operator may
    have changed the bounds."""
    low = int(engine.config.get("BRAIINS_POWER_MIN_W", 1500))
    high = int(engine.config.get("BRAIINS_POWER_MAX_W", 5000))
    # Guard against inverted / equal bounds
    if high <= low:
        high = low + 1
    engine.wattage_search_low = low
    engine.wattage_search_high = high


def _sample_at(engine, watt: int) -> dict | None:
    """Apply power-target *watt*, wait for BOS to settle, then record a sample.

    Sets power limit, waits BRAIINS_TUNER_STABILIZE_WAIT_SEC seconds in 1-s
    slices (bails out if engine.running flips False), then calls summary().
    Appends to engine.wattage_results and saves a checkpoint.  Returns the
    sample dict on success or None on any error.
    """
    stabilize_s = int(engine.config.get("BRAIINS_TUNER_STABILIZE_WAIT_SEC", 600))
    try:
        engine.api.set_power_limit(watt)
    except (MinerOfflineError, MinerCommandError) as exc:
        engine.log(f"Braiins: set_power_limit({watt} W) failed: {exc}", level="ERROR")
        return None

    engine.log(f"Braiins: power target set to {watt} W — settling {stabilize_s}s")
    deadline = time.time() + stabilize_s
    while engine.running and time.time() < deadline:
        time.sleep(1)

    if not engine.running:
        return None

    try:
        summary = engine.api.summary()
    except (MinerOfflineError, MinerCommandError) as exc:
        engine.log(f"Braiins: summary() failed after settle at {watt} W: {exc}", level="ERROR")
        return None

    hashrate_ths = summary.hashrate_ths
    power_w_actual = summary.power_w
    efficiency_jth = power_w_actual / hashrate_ths if hashrate_ths and hashrate_ths > 0 else None

    # Attempt profit calculation using current minerstat snapshot
    profit_usd_per_day: float | None = None
    try:
        rate, coin_data, modifier = get_profit_display_context(engine)
        if coin_data is not None:
            profit_usd_per_day = compute_profit_usd_per_day(
                hashrate_ths, power_w_actual, coin_data, rate, modifier
            )
    except Exception:  # noqa: BLE001
        pass

    sample = {
        "watt": watt,
        "hashrate_ths": hashrate_ths,
        "power_w_actual": power_w_actual,
        "efficiency_jth": efficiency_jth,
        "profit_usd_per_day": profit_usd_per_day,
        "fan_speed": summary.fan_speed,
        "ts": time.time(),
    }
    engine.wattage_results.append(sample)
    engine._save_checkpoint()

    eff_str = f"{efficiency_jth:.2f}" if efficiency_jth else "n/a"
    profit_str = f"${profit_usd_per_day:.4f}/day" if profit_usd_per_day is not None else "n/a"
    engine.log(
        f"Braiins: sample at {watt} W → "
        f"{hashrate_ths:.1f} TH/s, "
        f"{power_w_actual:.0f} W actual, "
        f"{eff_str} J/TH, "
        f"profit {profit_str}"
    )
    return sample


def _compute_sample_profit(engine, sample: dict) -> float:
    """Return a ranking scalar where HIGHER IS BETTER.

    Uses profit $/day when a Minerstat snapshot is available; falls back to
    negative J/TH (so lower wattage = higher rank when efficiency is the
    only signal).  Returns -inf for samples with missing / zero hashrate so
    they sort last.
    """
    if sample is None:
        return -math.inf
    hashrate_ths = sample.get("hashrate_ths")
    if not hashrate_ths or hashrate_ths <= 0:
        return -math.inf

    # Prefer live profit figure already stored on the sample
    profit = sample.get("profit_usd_per_day")
    if profit is not None:
        return float(profit)

    # Try to recompute with current minerstat snapshot (snapshot may have
    # arrived since the sample was captured)
    try:
        rate, coin_data, modifier = get_profit_display_context(engine)
        if coin_data is not None:
            profit = compute_profit_usd_per_day(
                hashrate_ths,
                sample.get("power_w_actual"),
                coin_data,
                rate,
                modifier,
            )
            if profit is not None:
                return float(profit)
    except Exception:  # noqa: BLE001
        pass

    # Fallback: negate J/TH (higher efficiency = less waste = higher rank)
    eff = sample.get("efficiency_jth")
    if eff is None or eff <= 0:
        return -math.inf
    return -float(eff)


def _recent_sample(engine, watt: int, tolerance_w: int) -> dict | None:
    """Return the most recent wattage_results entry within tolerance_w of
    *watt*, or None if no such entry exists."""
    best: dict | None = None
    for entry in engine.wattage_results:
        if abs(entry.get("watt", -9999) - watt) <= tolerance_w and (  # noqa: SIM102
            best is None or entry.get("ts", 0) > best.get("ts", 0)
        ):
            best = entry
    return best


def _narrow_bounds(engine, low_sample: dict, mid_sample: dict, high_sample: dict) -> None:
    """Apply the three-way comparison rule to shrink the search window.

    Mutates engine.wattage_search_low / engine.wattage_search_high in place.
    """
    low_w = engine.wattage_search_low
    high_w = engine.wattage_search_high
    mid_w = (low_w + high_w) // 2

    p_low = _compute_sample_profit(engine, low_sample)
    p_mid = _compute_sample_profit(engine, mid_sample)
    p_high = _compute_sample_profit(engine, high_sample)

    engine.log(
        f"Braiins bounds [{low_w}, {high_w}]: "
        f"p_low={p_low:.4f}, p_mid={p_mid:.4f}, p_high={p_high:.4f}",
        level="DEBUG",
    )

    if p_mid >= p_low and p_mid >= p_high:
        # Optimum brackets around mid — narrow the window toward mid
        delta = max((high_w - low_w) // 4, 1)
        engine.wattage_search_low = max(low_w, mid_w - delta)
        engine.wattage_search_high = min(high_w, mid_w + delta)
    elif p_high > p_low:
        engine.wattage_search_low = mid_w
    else:
        engine.wattage_search_high = mid_w

    engine.log(
        f"Braiins bounds narrowed to [{engine.wattage_search_low}, {engine.wattage_search_high}]",
        level="DEBUG",
    )


def _select_best(engine) -> int | None:
    """Return the watt value of the highest-profit entry in wattage_results,
    or None if the list is empty."""
    if not engine.wattage_results:
        return None
    best_entry = max(engine.wattage_results, key=lambda e: _compute_sample_profit(engine, e))
    return best_entry.get("watt")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_braiins_loop(engine) -> None:
    """Wattage binary-search loop for Braiins firmware miners.

    Phase sequence (one pass):
      1. PHASE_BRAIINS_DISCOVERY  — init bounds, ensure mining is active
      2. PHASE_BRAIINS_WATTAGE_SEARCH — binary-search until convergence
      3. PHASE_BRAIINS_PERPETUAL  — periodic re-sample at best wattage

    The outer `while engine.running` wraps the pass so that on bounds
    expansion (operator widened BRAIINS_POWER_MIN_W / BRAIINS_POWER_MAX_W)
    the perpetual phase breaks out and the outer loop re-enters discovery
    with the new bounds — without recursive call. Returns when
    engine.running flips False.
    """
    while engine.running:
        _run_braiins_pass(engine)


def _run_braiins_pass(engine) -> None:
    """One pass through discovery → search → perpetual. Returns either
    when engine.running flips False or when the perpetual phase breaks
    out for re-entry (e.g. bounds expanded)."""
    # ── Phase 1: Discovery init ──────────────────────────────────────────────
    engine.phase = engine.PHASE_BRAIINS_DISCOVERY
    engine.log("Braiins: starting wattage binary-search discovery")

    _init_search_bounds(engine)

    # Ensure BOS is actively mining so power-target adjustments take effect
    try:
        engine.api.start_mining()
        engine.log("Braiins: start_mining() sent")
    except MinerCommandError as exc:
        engine.log(f"Braiins: start_mining() non-fatal: {exc}", level="DEBUG")

    if not engine.running:
        return

    # ── Phase 2: Binary-search loop ─────────────────────────────────────────
    while engine.running:
        tolerance = int(engine.config.get("BRAIINS_BINARY_SEARCH_TOLERANCE_W", 100))

        if engine.wattage_search_high - engine.wattage_search_low <= tolerance:
            engine.log(
                f"Braiins: search converged "
                f"(range [{engine.wattage_search_low}, {engine.wattage_search_high}] "
                f"<= {tolerance} W tolerance)"
            )
            break

        low_w = engine.wattage_search_low
        high_w = engine.wattage_search_high
        mid_w = (low_w + high_w) // 2

        low_sample = _recent_sample(engine, low_w, tolerance)
        mid_sample = _recent_sample(engine, mid_w, tolerance)
        high_sample = _recent_sample(engine, high_w, tolerance)

        missing = []
        if low_sample is None:
            missing.append(low_w)
        if mid_sample is None:
            missing.append(mid_w)
        if high_sample is None:
            missing.append(high_w)

        if missing:
            # Sample the first missing wattage point
            target_w = missing[0]
            engine.phase = engine.PHASE_BRAIINS_WATTAGE_SEARCH
            engine.log(f"Braiins: sampling missing point {target_w} W")
            _sample_at(engine, target_w)
            continue

        # All three present — narrow the window
        _narrow_bounds(engine, low_sample, mid_sample, high_sample)
        engine.best_wattage_w = _select_best(engine)
        engine._save_profile()
        engine._save_checkpoint()

    if not engine.running:
        return

    # ── Post-search: apply best and save ────────────────────────────────────
    engine.best_wattage_w = _select_best(engine)
    if engine.best_wattage_w is not None:
        engine.log(f"Braiins: search complete — best wattage {engine.best_wattage_w} W")
        try:
            engine.api.set_power_limit(engine.best_wattage_w)
        except (MinerOfflineError, MinerCommandError) as exc:
            engine.log(
                f"Braiins: set_power_limit({engine.best_wattage_w} W) post-search failed: {exc}",
                level="ERROR",
            )
    else:
        engine.log("Braiins: no samples collected — staying at current wattage", level="WARN")

    engine._save_profile()

    # ── Phase 3: Perpetual monitoring loop ──────────────────────────────────
    while engine.running:
        engine.phase = engine.PHASE_BRAIINS_PERPETUAL

        # Sleep PERPETUAL_VOLTAGE_CHECK_MIN minutes in 1-second slices
        check_min = int(engine.config.get("PERPETUAL_VOLTAGE_CHECK_MIN", 10))
        sleep_s = check_min * 60
        engine.log(f"Braiins: perpetual — next sample in {check_min} min")
        deadline = time.time() + sleep_s
        while engine.running and time.time() < deadline:
            time.sleep(1)

        if not engine.running:
            break

        # Re-sample at best wattage (records in wattage_results for dashboard)
        best_w = engine.best_wattage_w
        if best_w is not None:
            _sample_at(engine, best_w)
            engine._save_profile()

        # Re-check bounds: if operator widened BRAIINS_POWER_MAX_W or
        # BRAIINS_POWER_MIN_W such that the new range extends more than
        # tolerance past the current best, reset bounds and re-enter search.
        # TODO(follow-up): add profit-regression detection — if current
        # efficiency degrades significantly vs. the best known sample,
        # trigger a re-search automatically.
        tolerance = int(engine.config.get("BRAIINS_BINARY_SEARCH_TOLERANCE_W", 100))
        prev_low = engine.wattage_search_low
        prev_high = engine.wattage_search_high
        _init_search_bounds(engine)
        new_low = engine.wattage_search_low
        new_high = engine.wattage_search_high
        best_w = engine.best_wattage_w or 0

        unexplored_low = best_w - new_low > tolerance and new_low < prev_low
        unexplored_high = new_high - best_w > tolerance and new_high > prev_high

        if unexplored_low or unexplored_high:
            engine.log(
                f"Braiins: bounds expanded "
                f"([{new_low}, {new_high}] vs prev [{prev_low}, {prev_high}]) — "
                "re-entering search"
            )
            # Break perpetual loop → falls back into the outer search loop
            break

        # Restore bounds so they don't drift from the re-clamp above
        engine.wattage_search_low = prev_low
        engine.wattage_search_high = prev_high

    # Returning here either means engine.running flipped False (outer loop
    # exits cleanly) or the perpetual phase broke out for bounds expansion
    # (outer loop re-enters discovery with new bounds).
