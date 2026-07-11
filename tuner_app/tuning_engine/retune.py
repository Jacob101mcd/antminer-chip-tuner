"""
Voltage retune operations for the tuning engine.
This module handles retuning at specific voltages, including
extended retunes from vf_surface data and fine-grid + retune workflows.
"""

from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime

from tuner_app.miner.exceptions import MinerCommandError, MinerNotReady


def start_retune(engine, voltage_mv):
    """Launch a single-voltage retune in the engine's worker thread.
    Accepts voltages that already have a voltage_results entry (classic
    retune — replace in place) AND voltages that only exist in vf_surface
    (R7 extended retune — a "Retune this voltage" action from a coarse/fine
    row in the Highest Efficiency Tunes list). Returns (ok: bool, err: str)."""
    with engine._control_lock:
        if engine.thread and engine.thread.is_alive():
            return False, "engine is busy — stop the current tune first"
        has_results_entry = any(r.get("voltage_mv") == voltage_mv for r in engine.voltage_results)
        has_vf_entry = any(
            e.get("voltage_mv") == voltage_mv and e.get("efficiency_jth") is not None
            for e in engine.vf_surface
        )
        if not has_results_entry and not has_vf_entry:
            return False, (f"no data for {voltage_mv} mV — measure it via Phase V first")
        # Wipe log entries tagged with this voltage before the retune starts —
        # operators opened Retune expecting a fresh per-step log, not the
        # previous tune's entries layered under the new ones.
        engine.clear_log_entries_for_voltage(voltage_mv)
        engine.running = True
        engine.thread = threading.Thread(
            target=engine._retune_runner, args=(voltage_mv,), daemon=True
        )
        engine.thread.start()
        return True, ""


def retune_runner(engine, voltage_mv):
    # MRR: flip rig to disabled — retune measures hashrate across several
    # freq settings, so the advertised rate is temporarily meaningless.
    # The dynamic loop's monitor entry re-fires "maintaining" once the
    # retune completes and find_next_* all return None.
    engine._mrr_phase6_announced = False
    engine._mrr_polish_announced = False
    engine._mrr_sync("tuning", reason=f"Retune at {voltage_mv} mV")
    # Retune touched voltage_results — clear tune_complete so the next
    # monitor entry re-saves the profile + fires MRR maintaining.
    engine.tuning_complete = False
    try:
        # Tag entries during retune so the per-voltage modal shows the
        # new tune's logs. Cleared below regardless of outcome.
        engine.current_sweep_voltage_mv = voltage_mv
        engine.retune_voltage(voltage_mv)
    except Exception as ex:
        engine.phase = engine.PHASE_ERROR
        engine.phase_detail = f"Retune failed: {ex}"
        engine.log(f"Retune failed: {ex}")
        engine.log(traceback.format_exc())
        return
    finally:
        engine.current_sweep_voltage_mv = None
    if not engine.running:
        return
    # Hand back to the dynamic loop. It will see baseline done, no pending
    # find_next_* work (assuming the prior tune was complete), fall to
    # monitor mode, fire MRR maintaining, and run perpetual cycles. If a
    # prior tune left exploration work pending, the dynamic loop picks it
    # up and continues — exactly the behavior we want.
    try:
        engine._run_inner()
    except Exception as ex:
        engine.log(f"Dynamic loop re-entry after retune failed: {ex}")
        engine.log(traceback.format_exc())


def start_fine_then_retune(engine, voltage_mv, freq_mhz):
    """Launch the "coarse-winner → fine-grid → retune" action in the
    engine's worker thread. Used by the profit recompute apply endpoint
    when a coarse-cell winner needs its fine grid filled in before
    chip-tuning. Returns (ok: bool, err: str)."""
    with engine._control_lock:
        if engine.thread and engine.thread.is_alive():
            return False, "engine is busy — stop the current tune first"
        engine.running = True
        engine.thread = threading.Thread(
            target=engine._fine_then_retune_runner, args=(voltage_mv, freq_mhz), daemon=True
        )
        engine.thread.start()
        return True, ""


def fine_then_retune_runner(engine, voltage_mv, freq_mhz):
    """Workflow for a coarse-cell winner: Phase 0 prelude → fine grid
    around (voltage_mv, freq_mhz) → R7 extended retune at voltage_mv
    (which picks up the fresh fine data as the seed) → Phase 6.

    The retune inherits its seed from the best-scoring fine cell at
    voltage_mv because R7 extended retune always picks `min(vf_candidates,
    key=self._score_key())` — and by the time we get there, vf_surface
    contains the fine cells we just measured."""
    try:
        engine.current_sweep_voltage_mv = voltage_mv
        engine._phase0_discovery()
        if not engine.running:
            return
        engine._fine_grid_around_cell(voltage_mv, freq_mhz)
        if not engine.running:
            return
        engine.retune_voltage(voltage_mv)
    except Exception as ex:
        engine.phase = engine.PHASE_ERROR
        engine.phase_detail = f"Fine+retune failed: {ex}"
        engine.log(f"Fine+retune failed: {ex}")
        engine.log(traceback.format_exc())
        return
    finally:
        engine.current_sweep_voltage_mv = None
    if not engine.running:
        return
    # Hand back to the dynamic loop — see _retune_runner's tail comment.
    engine.tuning_complete = False
    try:
        engine._run_inner()
    except Exception as ex:
        engine.log(f"Dynamic loop re-entry after fine+retune failed: {ex}")
        engine.log(traceback.format_exc())


def restart_between_probes(engine):
    """Miner stop/start between voltage probes (or before Phase 3) so a
    failing probe doesn't leave chips in a degraded state that poisons the
    next probe. Uses the same RESET_STOP_WAIT / RESET_START_WAIT knobs as
    Phase 3's between-round restart."""
    engine.phase_detail = "Restart: stop_mining"
    engine.log("Restart: stop_mining")
    try:
        engine.api.stop_mining()
    except MinerCommandError as e:
        engine.log(f"stop_mining failed during restart: {e} — continuing anyway")
    stop_wait = engine.config["RESET_STOP_WAIT"]
    remaining = stop_wait
    while remaining > 0 and engine.running:
        time.sleep(min(remaining, 10))
        remaining -= 10
    if not engine.running:
        return
    engine.phase_detail = "Restart: start_mining"
    engine.log("Restart: start_mining")
    engine.api.start_mining()
    start_wait = engine.config["RESET_START_WAIT"]
    remaining = start_wait
    while remaining > 0 and engine.running:
        time.sleep(min(remaining, 10))
        remaining -= 10
    if not engine.running:
        return
    engine._wait_for_mining_state(timeout=600)


def refresh_sweep_reference(engine):
    """Populate sweep_voltage_mv / sweep_hashrate_ths / sweep_freq_arrays from
    the voltage_results entry pointed to by active_sweep_voltage_mv. If
    active_sweep_voltage_mv is None, defaults to the best-scoring entry
    (most-efficient in efficiency mode, most-profitable in profit mode).

    NOTE: this only sets the default when no explicit override exists.
    Phase 6 does NOT re-evaluate the scoring on every cycle — voltage
    changes in maintenance mode are gated behind the fleet-wide profit
    recompute button / scheduled day, per the demand-charge constraint."""
    if not engine.voltage_results:
        return
    best = min(engine.voltage_results, key=engine._score_key())
    target_mv = engine.active_sweep_voltage_mv
    if target_mv is None:
        entry = best
        engine.active_sweep_voltage_mv = entry.get("voltage_mv")
    else:
        entry = next((r for r in engine.voltage_results if r.get("voltage_mv") == target_mv), None)
        if entry is None:
            # Override voltage no longer exists (voltage_results may have been trimmed) — fall back.
            entry = best
            engine.active_sweep_voltage_mv = entry.get("voltage_mv")
    engine.sweep_voltage_mv = entry.get("voltage_mv", 0)
    engine.sweep_hashrate_ths = entry.get("hashrate_ths", 0.0) or 0.0
    # Old profiles may not have stable_freq_arrays per entry — backfill from current state.
    entry_freqs = entry.get("stable_freq_arrays")
    if entry_freqs and any(entry_freqs):
        engine.sweep_freq_arrays = [list(arr) for arr in entry_freqs]
    elif any(engine.stable_freq_arrays):
        engine.sweep_freq_arrays = [list(arr) for arr in engine.stable_freq_arrays]


def select_voltage_profile(engine, voltage_mv):
    """Switch the active sweep profile to a different voltage_results entry.
    Updates sweep reference, resets voltage_adjustment_mv, applies voltage +
    chip freqs. Safe to call while Phase 6 is in its sleep interval.

    Duplicate-voltage handling: top-K selection can produce two
    voltage_results entries at the same voltage (different seed_f_mhz).
    When duplicates exist at `voltage_mv`, we pick the best-scoring match
    using the active target mode's `_score_key`. The UI can still address
    a specific entry via retune / chip-tune workflow; operators wanting
    to switch between duplicate-voltage entries should use the per-row
    Use Profile button, which drives through this path one row at a time.
    """
    matches = [r for r in engine.voltage_results if r.get("voltage_mv") == voltage_mv]
    if not matches:
        raise ValueError(f"No voltage_results entry for {voltage_mv} mV")
    entry = min(matches, key=engine._score_key()) if len(matches) > 1 else matches[0]

    engine.active_sweep_voltage_mv = voltage_mv
    engine.sweep_voltage_mv = entry.get("voltage_mv", voltage_mv)
    engine.sweep_hashrate_ths = entry.get("hashrate_ths", 0.0) or 0.0
    entry_freqs = entry.get("stable_freq_arrays") or engine._empty_board_arrays()
    engine.sweep_freq_arrays = [list(arr) for arr in entry_freqs]
    entry_baseline = entry.get("baseline_scores")
    if entry_baseline:
        engine.baseline_scores = [list(arr) for arr in entry_baseline]
    engine.voltage_adjustment_mv = 0
    engine.stable_freq_arrays = [list(arr) for arr in engine.sweep_freq_arrays]
    engine.min_voltage_mv = engine.sweep_voltage_mv

    engine.log(
        f"Switched active sweep profile to {voltage_mv} mV "
        f"(reference hashrate {engine.sweep_hashrate_ths:.2f} TH/s)"
    )
    try:
        engine._apply_stable_freqs()
        engine._wait_for_mining_state(timeout=600)
        engine._save_profile()
    except (MinerCommandError, MinerNotReady) as ex:
        engine.log(f"select_voltage_profile apply failed: {ex}")
        raise
    # MRR: re-push advertised hashrate when the operator swaps profiles
    # while Phase 6 is active (sweep_hashrate_ths changed, so the MRR ad
    # needs to follow). If Phase 6 isn't running yet this is a no-op
    # (flag is False and _mrr_sync is gated on MRR_ENABLED anyway).
    if engine._mrr_phase6_announced:
        engine._mrr_sync("maintaining", reason=f"Profile switched to {voltage_mv} mV")


def retune_voltage(engine, voltage_mv):
    """Re-run Phases 1-4 at a single voltage, replacing the voltage_results
    entry for that voltage (or appending a new one if this voltage only
    exists in vf_surface — R7 extended retune). Caller is responsible for
    ensuring no other tuning thread is active.

    Duplicate-voltage handling: if multiple voltage_results entries exist
    at `voltage_mv` (possible when top-K cell selection picked two cells
    at the same voltage with different seed_f), the best-scoring match
    is replaced. To retune a specific sibling entry instead, use the
    per-row Retune button which currently passes voltage only — in that
    case the best entry is kept "live" and the re-tune overwrites it."""
    matches = [
        (i, r) for i, r in enumerate(engine.voltage_results) if r.get("voltage_mv") == voltage_mv
    ]
    if matches:
        idx, prior = min(matches, key=lambda p: engine._score_key()(p[1]))
    else:
        idx, prior = None, None

    # R7 extended retune: voltage might not have a voltage_results entry
    # yet — derive a seed_f from the best-scoring vf_surface cell at that
    # voltage. "Best" respects the active target mode via _score_key, so a
    # profit-mode retune seeds from the most-profitable surface cell at
    # that voltage (not necessarily the lowest-J/TH one).
    vf_seed_entry = None
    if prior is None:
        vf_candidates = [
            e
            for e in engine.vf_surface
            if e.get("voltage_mv") == voltage_mv and e.get("efficiency_jth") is not None
        ]
        if not vf_candidates:
            raise ValueError(f"No voltage_results or vf_surface data at {voltage_mv} mV")
        vf_seed_entry = min(vf_candidates, key=engine._score_key())
        engine.log(
            f"Retune (extended): no voltage_results entry at "
            f"{voltage_mv} mV — seeding from best vf_surface cell at "
            f"{vf_seed_entry['freq_mhz']:.1f} MHz "
            f"({vf_seed_entry['efficiency_jth']:.2f} J/TH)"
        )
    else:
        engine.log(f"Retune: re-running Phases 1-4 at {voltage_mv} mV")

    engine.parked_chips = [set() for _ in range(engine.num_boards)]
    engine.profiling_round = 0
    engine.profiling_completion_pct = 0.0
    engine.chips_stable_pct = 0.0
    engine.chips_converged = 0
    engine.chips_alive = 0
    engine.stillness_streak = 0
    engine.chip_max = None  # Per-chip "lowest known-unstable" memory rebuilt by _phase3_profiling.
    engine.phase3_active = False

    engine._phase0_discovery()
    if not engine.running:
        return

    engine.min_voltage_mv = voltage_mv
    if prior is not None:
        # Prefer Phase V's seed_f_mhz from the prior entry so retune lands
        # in the same neighborhood as the initial tune. Fallback to the
        # prior tuned avg freq when the entry predates Phase V.
        seed_f = prior.get("seed_f_mhz") or prior.get("avg_freq_mhz")
        if not seed_f:
            raise MinerCommandError(
                f"Retune: voltage_results entry at {voltage_mv} mV has neither "
                f"seed_f_mhz nor avg_freq_mhz — cannot derive a Phase 1 freq"
            )
    else:
        seed_f = float(vf_seed_entry["freq_mhz"])
    engine._phase1_set_voltage(voltage_mv, seed_f)
    if not engine.running:
        return

    clocks_data = engine.api.clocks()
    if clocks_data:
        engine.stable_freq_arrays = [[] for _ in range(engine.num_boards)]
        engine.proposed_freqs = [[] for _ in range(engine.num_boards)]
        for board in clocks_data:
            bidx = board.index
            if 0 <= bidx < engine.num_boards:
                engine.stable_freq_arrays[bidx] = list(board.chip_freqs_mhz)
                engine.proposed_freqs[bidx] = list(board.chip_freqs_mhz)

    # Retune skips Phase 2 (baseline_scores were collected by the original
    # sweep at this voltage and are still valid), but it still needs to
    # re-derive parked_chips since we just reset that above. Without this
    # call, dead chips would fall through Phase 3's stable branch and get
    # tuned up. Then snap alive chips to seed_f on the 3.125 MHz grid so
    # the iterative loop's first round operates on a consistent starting
    # frequency.
    engine._park_dead_chips_from_baseline()
    grid = 3.125
    seed_snap = round(float(seed_f) / grid) * grid
    for b in range(engine.num_boards):
        for i in range(len(engine.stable_freq_arrays[b])):
            if i in engine.parked_chips[b]:
                continue
            engine.stable_freq_arrays[b][i] = seed_snap

    # Retune resets polish state so the new measurement runs through a
    # clean Phase 3 → Phase 3b pipeline, matching the original tune path.
    engine.polish_round = 0
    engine.polish_active = False
    engine._phase3_profiling(seed_f)
    if not engine.running:
        return
    engine._phase3b_polish()
    if not engine.running:
        return
    efficiency = engine._phase4_measure_efficiency()
    if not engine.running or efficiency is None:
        raise RuntimeError("Phase 4 efficiency measurement failed during retune")

    avg_freq = 0
    total_chips = 0
    for b in range(engine.num_boards):
        if engine.stable_freq_arrays[b]:
            avg_freq += sum(engine.stable_freq_arrays[b])
            total_chips += len(engine.stable_freq_arrays[b])
    if total_chips > 0:
        avg_freq /= total_chips

    # Synthesize a vf_source for R7 extended retune so the cell popup's
    # before/after block has valid data even when there was no prior
    # voltage_results entry.
    if prior is not None:
        synth_vf_source = prior.get("vf_source")
        from_vf = bool(prior.get("from_vf_exploration"))
    elif vf_seed_entry is not None:
        synth_vf_source = {
            "kind": "fine" if vf_seed_entry.get("fine") else "coarse",
            "voltage_mv": int(vf_seed_entry["voltage_mv"]),
            "freq_mhz": round(float(vf_seed_entry["freq_mhz"]), 3),
            "coarse_jth": float(vf_seed_entry["efficiency_jth"]),
            "hashrate_ths": vf_seed_entry.get("hashrate_ths"),
            "power_w": vf_seed_entry.get("power_w"),
        }
        from_vf = True
    else:
        synth_vf_source = None
        from_vf = False

    new_result = {
        "voltage_mv": voltage_mv,
        "efficiency_jth": efficiency["efficiency_jth"],
        "hashrate_ths": efficiency["hashrate_ths"],
        "power_w": efficiency["power_w"],
        "avg_freq_mhz": avg_freq,
        "duration_sec": 0,
        "per_board": efficiency.get("per_board", []),
        "measured_at": datetime.now().isoformat(),
        "stable_freq_arrays": [arr[:] for arr in engine.stable_freq_arrays],
        "baseline_scores": [arr[:] for arr in engine.baseline_scores],
        "retuned": True,
        "from_vf_exploration": from_vf,
        "seed_f_mhz": float(seed_f) if seed_f else (prior.get("seed_f_mhz") if prior else None),
        # Preserve vf_source across retune so the cell popup's before/after
        # link stays accurate — retune doesn't change which Phase V cell
        # seeded this voltage.
        "vf_source": synth_vf_source,
    }
    if idx is not None:
        engine.voltage_results[idx] = new_result
    else:
        engine.voltage_results.append(new_result)
        idx = len(engine.voltage_results) - 1
    best = min(engine.voltage_results, key=lambda r: r.get("efficiency_jth", float("inf")))
    engine.best_efficiency = best.get("efficiency_jth")

    if engine.active_sweep_voltage_mv == voltage_mv:
        engine._refresh_sweep_reference()
        engine.voltage_adjustment_mv = 0

    engine._save_profile()
    engine.log(
        f"Retune at {voltage_mv} mV complete: "
        f"{efficiency['hashrate_ths']:.2f} TH/s @ "
        f"{efficiency['efficiency_jth']:.2f} J/TH"
    )
