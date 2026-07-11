"""Reset helpers: dead-chip parking from baseline + post-recovery V/F reset."""

from __future__ import annotations

import statistics

from tuner_app.miner.exceptions import MinerCommandError, MinerNotReady


def reset_to_safe_vf(engine):
    """Apply BASELINE_VOLTAGE_MV + BASELINE_FREQ so the miner comes out
    of any stale/unstable V/F the firmware was carrying forward from
    whatever caused the recovery. Idempotent; safe to call when the
    miner is already at those values.
    """
    safe_v = int(engine.config.get("BASELINE_VOLTAGE_MV", 15100))
    safe_f = float(engine.config.get("BASELINE_FREQ", 200))
    engine.log(f"Post-recovery reset: applying safe V/F ({safe_v} mV, {safe_f:.1f} MHz)")
    try:
        engine._phase1_set_voltage(safe_v, safe_f)
    except (MinerCommandError, MinerNotReady) as e:
        # Don't swallow — the retry loop will see this and treat it as
        # another recoverable failure. But log first so operators can
        # see what happened.
        engine.log(f"Post-recovery reset failed: {e}")
        raise


def park_dead_chips_from_baseline(engine):
    """Derive parked_chips from baseline_scores.

    Called once per tune: at the end of Phase 2 on a fresh run, or at the
    start of a retune (which reuses existing baseline rather than
    re-collecting). A chip whose averaged baseline score is at or below
    DEAD_CHIP_SCORE is parked at DEAD_CHIP_FREQ — the iterative Phase 3
    loop skips chips in `parked_chips[b]` entirely so they never enter the
    spread-cap calculation or get a chance to drift.

    Low scores DURING Phase 3 are treated as instability (unstable branch
    steps the chip down), never as deadness — the averaged Phase 2 reading
    is the sole authority on whether a chip is actually dead.
    """
    # Bixbit sentinel: chips_per_board==0 means no per-chip data is
    # available (Bixbit auto-tunes internally). Nothing to park.
    if engine.chips_per_board == 0:
        return

    dead_score = engine.config["DEAD_CHIP_SCORE"]
    dead_chip_freq = engine.config["DEAD_CHIP_FREQ"]

    for b in range(engine.num_boards):
        if not engine.baseline_scores[b]:
            continue
        n_chips = len(engine.baseline_scores[b])
        avg_score = statistics.mean(engine.baseline_scores[b])
        dead_chips = [i for i, s in enumerate(engine.baseline_scores[b]) if s <= dead_score]
        for i in dead_chips:
            engine.parked_chips[b].add(i)
            if i < len(engine.proposed_freqs[b]):
                engine.proposed_freqs[b][i] = dead_chip_freq
            if i < len(engine.stable_freq_arrays[b]):
                engine.stable_freq_arrays[b][i] = dead_chip_freq
        engine.log(
            f"Board {b} baseline: avg score={avg_score:.1f}, chips={n_chips}"
            + (f", dead chips: {dead_chips}" if dead_chips else "")
        )


# ─── Scope-aware reset helpers (used by Reset Profile partial scopes) ──────────
#
# Each helper zeroes out the engine state for a specific phase boundary so the
# operator can pick "redo chip-tune", "redo fine + chip-tune", or "redo Phase V
# onwards" without losing earlier work. Called from
# tuner_app/manager/bulk._delete_profile_for_ip(scope=...) before the
# scope-correct checkpoint is rewritten to disk.


def reset_chip_tuning_fields(engine):
    """Zero out Phase 3 / 3b / 4 outputs on an engine, leaving Phase V
    results, baseline, and stock intact. Next Start Tuning resumes at the
    top-K refinement loop (starting from index 0)."""
    engine.voltage_results = []
    engine.stable_freq_arrays = engine._empty_board_arrays()
    engine.proposed_freqs = engine._empty_board_arrays()
    engine.profiling_round = 0
    engine.profiling_completion_pct = 0.0
    engine.chips_stable_pct = 0.0
    engine.chips_converged = 0
    engine.chips_alive = 0
    engine.stillness_streak = 0
    engine.chip_max = None
    engine.phase3_active = False
    engine.polish_round = 0
    engine.polish_active = False
    engine.vf_refinement_index = None
    # Clear the dynamic-state-machine in-flight marker too — without this, a
    # partial-scope chip-tune reset would leave in_flight_chip_tune_target set
    # on disk, and the next Start Tuning would resume into a chip-tune whose
    # stable_freq_arrays was just wiped to empty.
    engine.in_flight_chip_tune_target = None
    engine.parked_chips = [set() for _ in range(engine.num_boards)]
    engine.best_efficiency = None
    engine.tuning_complete = False
    engine.active_sweep_voltage_mv = None
    engine.sweep_voltage_mv = 0
    engine.sweep_hashrate_ths = 0.0
    engine.sweep_freq_arrays = [[] for _ in range(engine.num_boards)]
    engine.voltage_adjustment_mv = 0
    engine.last_restart_ts = None
    engine.current_step_started_at = None
    engine.current_sweep_voltage_mv = None


def reset_fine_grid_fields(engine):
    """Drop fine-grid entries from vf_surface and clear the top-K selection
    so Phase V re-runs its walk + fine grids + top-K refinement. Coarse cells
    are preserved; the ray walk reuses them via the (V, round(F, 3)) lookup.

    Converted anchor cells (entries with `fine: True` whose `coarse_anchor`
    self-points to the entry's own (V, F)) are un-converted back to coarse:
    `fine` flips False, `kind` and `coarse_anchor` are stripped. The original
    measurement carried into the fine grid was the coarse measurement (reused,
    not re-taken), so dropping these entries would force the engine to
    re-measure data we already have. Un-converting preserves it.

    Also clears the fine-anchor since fine data is gone."""
    new_surface = []
    for e in engine.vf_surface:
        if not e.get("fine"):
            new_surface.append(e)
            continue
        ca = e.get("coarse_anchor") or {}
        try:
            ca_v = int(ca.get("voltage_mv", -1))
            ca_f = round(float(ca.get("freq_mhz", -1)), 3)
            self_v = int(e["voltage_mv"])
            self_f = round(float(e["freq_mhz"]), 3)
        except (KeyError, TypeError, ValueError):
            ca_v = ca_f = self_v = self_f = None
        if ca_v == self_v and ca_f is not None and ca_f == self_f:
            # Converted anchor — un-convert.
            uncv = {k: v for k, v in e.items() if k not in ("kind", "coarse_anchor")}
            uncv["fine"] = False
            new_surface.append(uncv)
        # else: a true fine sub-cell — drop.
    engine.vf_surface = new_surface
    engine.vf_top_k_voltages = []
    engine.vf_planned_grid = []
    engine.vf_skipped = []
    engine.vf_fine_anchor = None
    engine.vf_coarse_rays_checked = []
    engine.current_vf_point = None


def reset_coarse_grid_fields(engine):
    """Drop every vf_surface entry plus queued remeasurements (which point
    at cells that no longer exist). Phase V re-runs from scratch — also
    clears the fine-anchor and R5 ray-check state since the surface it
    references is gone."""
    engine.vf_surface = []
    engine.vf_planned_grid = []
    engine.vf_skipped = []
    engine.vf_fine_anchor = None
    engine.vf_coarse_rays_checked = []
    engine.remeasure_queue = []
    engine.current_vf_point = None
