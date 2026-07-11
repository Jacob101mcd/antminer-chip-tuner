"""
Free functions extracted from TuningEngine class methods for chip tuning loop operations.
Contains implementations of health sampling, profiling, and polish phases.
"""

from __future__ import annotations

import statistics
import time

from tuner_app.constants import FIRMWARE_FREQ_MIN_MHZ


def collect_chip_health_samples(engine, num_samples, interval, label):
    """Collect `num_samples` per-chip health snapshots at `interval`-second
    spacing, return averaged per-board lists. Falls back to baseline_scores
    on a board if no samples were collected (API error or board offline).
    Returns None if `engine.running` flips to False mid-collection.

    Returns the sentinel dict `{"thermal_retry": True}` when a thermal
    emergency was handled mid-collection. The caller (Phase 3 / 3b)
    should re-stabilize and re-sample the round — partial samples
    collected against unstable chips are not useful as a health score.

    Returns `{"restart_round": True}` when the miner stops hashing
    mid-collection and recovers via _wait_for_mining_state. Firmware
    may have lost per-chip clocks during the break and the chips are
    in a transient post-recovery window; the caller restarts the round
    so the per-round preamble (apply freqs, optional reset cycle, full
    STABILIZE_WAIT) re-runs before fresh sample collection.

    `label` is a human-readable phase tag prepended to phase_detail (e.g.
    "Round 3" or "Polish round 2") so the dashboard shows progress.
    """
    engine.phase_detail = f"{label}: collecting health scores"
    health_accum = [None] * engine.num_boards
    collected = 0
    while collected < num_samples and engine.running:
        em = engine._detect_thermal_emergency()
        if em:
            engine._handle_thermal_in_chip_tune(em)
            # Discard partial samples — re-stabilize required before
            # this round's health window can produce a clean score.
            return {"thermal_retry": True}
        if not engine._is_miner_hashing():
            engine.log(f"Miner not hashing during {label.lower()}, waiting...")
            engine._wait_for_mining_state(timeout=600)
            # Discard partial samples — firmware may have lost per-chip
            # clocks during the hashing break and the chips are now in a
            # transient post-recovery window. Caller re-runs the round
            # preamble (apply freqs, optional reset cycle, full
            # STABILIZE_WAIT) before fresh sample collection.
            return {"restart_round": True}
        hashrate_data = engine.api.hashrate()
        if not hashrate_data:
            time.sleep(interval)
            continue
        for board in hashrate_data:
            idx = board.index
            if idx >= engine.num_boards:
                continue
            chip_scores = board.health_pct
            if health_accum[idx] is None:
                health_accum[idx] = [0.0] * len(chip_scores)
            for i, s in enumerate(chip_scores):
                if i < len(health_accum[idx]):
                    health_accum[idx][i] += s
        collected += 1
        if collected < num_samples:
            time.sleep(interval)
    if not engine.running:
        return None

    health_scores = [None] * engine.num_boards
    for b in range(engine.num_boards):
        if health_accum[b] and collected > 0:
            health_scores[b] = [s / collected for s in health_accum[b]]
        else:
            health_scores[b] = (
                engine.baseline_scores[b][:] if b < len(engine.baseline_scores) else []
            )
    return health_scores


def phase3_profiling(engine, seed_f_mhz):
    """Iterative per-chip frequency tuning by health feedback.

    Each round samples per-chip health and, for each alive chip:
      - score >= baseline - CHIP_TUNE_UP_TOLERANCE -> step UP one CHIP_TUNE_STEP
      - score <  baseline - CHIP_TUNE_DOWN_TOLERANCE -> step DOWN one step
      - otherwise (in the hold band): no move

    Loop exits after CHIP_TUNE_STILLNESS_STREAK consecutive rounds with
    zero moves (defense against single-round noise faking convergence) or
    when MAX_PROFILING_ROUNDS is hit.

    Bounds (composed of two independent contracts):
      1. Hard F clamp: every chip stays inside [VF_EXPLORE_F_MIN,
         VF_EXPLORE_F_MAX] (the operator's coarse-grid range), with
         FIRMWARE_FREQ_MIN_MHZ as a defense floor in case F_MIN is set
         below 50.
      2. Relative spread cap: per-board, post-move
         `max(alive) - min(alive) <= CHIP_FREQ_SPREAD_MHZ`, evaluated
         live each round on the running state. This naturally tracks
         cohort drift — chips can step up together as long as they stay
         within SPREAD apart, and they collectively stop at F_MAX.
    Move attempts that would violate either contract are silently
    rejected (chip keeps its current freq, no chips_moved increment) —
    visible in the round summary as a chip pinned at the edge.

    seed_f is the *initial* uniform position of all alive chips (set by
    Phase 1's set_clock_all). It does not constrain the iteration's
    reachable space — only the F grid + SPREAD cap do.

    Dead chips (in `parked_chips[b]`, identified by Phase 2 baseline) are
    skipped entirely: they never get health-evaluated and don't enter the
    spread metric. This is the only protection against a dead chip pinned
    at DEAD_CHIP_FREQ (50 MHz) blowing the spread cap for the population.

    State: `stable_freq_arrays` IS the per-chip frequency state. Resume
    reads it from the checkpoint and continues the loop. `profiling_round`
    and `stillness_streak` round-trip too so a mid-loop crash picks up
    near where it left off rather than restarting at round 1.

    Preconditions: baseline_scores populated, parked_chips populated,
    stable_freq_arrays seeded for alive chips at seed_f (done by
    _run_phase3_phase4_at_voltage on fresh_start, or by the resume seed
    defense in _do_chip_tune_atomic on resume).
    """
    engine.phase = engine.PHASE_PROFILING
    grid = 3.125  # BM1368 firmware clock quantization (MHz)
    spread_cfg = int(engine.config["CHIP_FREQ_SPREAD_MHZ"])
    f_min_eff = max(
        round(float(engine.config["VF_EXPLORE_F_MIN"]) / grid) * grid,
        float(FIRMWARE_FREQ_MIN_MHZ),
    )
    f_max_eff = round(float(engine.config["VF_EXPLORE_F_MAX"]) / grid) * grid

    step = float(engine.config["CHIP_TUNE_STEP_MHZ"])
    step = max(grid, round(step / grid) * grid)
    up_tol = float(engine.config["CHIP_TUNE_UP_TOLERANCE"])
    down_tol = float(engine.config["CHIP_TUNE_DOWN_TOLERANCE"])
    streak_target = int(engine.config["CHIP_TUNE_STILLNESS_STREAK"])
    max_rounds = int(engine.config["MAX_PROFILING_ROUNDS"])
    num_samples = int(engine.config["ROUND_SAMPLES"])
    interval = int(engine.config["ROUND_INTERVAL"])
    stabilize_time = int(engine.config["STABILIZE_WAIT"])

    resuming = engine.phase3_active and engine.profiling_round > 0
    if resuming:
        engine.log(
            f"Phase 3: Resuming iterative chip-tune from round "
            f"{engine.profiling_round} (seed_f={seed_f_mhz:.1f} MHz, "
            f"F bounds [{f_min_eff:.0f}..{f_max_eff:.0f}] MHz, "
            f"max inter-chip spread {spread_cfg} MHz, "
            f"stillness {engine.stillness_streak}/{streak_target})"
        )
    else:
        engine.profiling_round = 0
        engine.stillness_streak = 0
        engine.log(
            f"Phase 3: Iterative chip-tune — seed_f={seed_f_mhz:.1f} MHz, "
            f"F bounds [{f_min_eff:.0f}..{f_max_eff:.0f}] MHz, "
            f"max inter-chip spread {spread_cfg} MHz, "
            f"step ±{step:.3f} MHz, "
            f"UP_TOL={up_tol:.0f} DOWN_TOL={down_tol:.0f}, "
            f"max_rounds={max_rounds}, stillness_target={streak_target}"
        )
    engine.phase3_active = True

    # Initialize chip_max if missing (fresh start) or shape-mismatched
    # (cross-hardware). None for each alive chip = "no known unstable
    # freq yet" so UP is unconstrained. Dead chips' entries are unused
    # since the move loop skips them entirely.
    need_init_max = (
        engine.chip_max is None
        or len(engine.chip_max) != engine.num_boards
        or any(
            len(engine.chip_max[b]) != len(engine.stable_freq_arrays[b])
            for b in range(engine.num_boards)
            if b < len(engine.chip_max)
        )
    )
    if need_init_max:
        engine.chip_max = [
            [None for _ in engine.stable_freq_arrays[b]] for b in range(engine.num_boards)
        ]

    while engine.running and engine.profiling_round < max_rounds:
        engine.profiling_round += 1

        # Per-round preamble — re-apply current stable_freq_arrays
        # unconditionally so any firmware reset is corrected before the
        # sampling window starts. Idempotent if firmware already matches.
        engine.phase_detail = f"Round {engine.profiling_round}: applying current freqs"
        engine._apply_stable_freqs()
        engine._wait_for_mining_state(timeout=600)

        if not engine.config["SKIP_ROUND_RESTART"]:
            engine.phase_detail = f"Round {engine.profiling_round}: reset cycle"
            engine.api.stop_mining()
            time.sleep(engine.config["RESET_STOP_WAIT"])
            engine.api.start_mining()
            time.sleep(engine.config["RESET_START_WAIT"])
            engine.phase_detail = f"Round {engine.profiling_round}: waiting for stabilization"
            engine._wait_for_mining_state(timeout=600)
            # Belt-and-suspenders re-apply post-restart: stop/start may not
            # persist per-chip clocks across the cycle. No-op if it does.
            engine.phase_detail = f"Round {engine.profiling_round}: re-applying freqs post-restart"
            engine._apply_stable_freqs()

        # Thermal stabilization before sampling.
        remaining = stabilize_time
        while remaining > 0 and engine.running:
            engine.phase_detail = (
                f"Round {engine.profiling_round}: chip stabilization ({remaining}s remaining)"
            )
            time.sleep(min(remaining, 10))
            remaining -= 10
            engine._update_live_data()
            em = engine._detect_thermal_emergency()
            if em:
                engine._handle_thermal_in_chip_tune(em)
        if not engine.running:
            break

        # Checkpoint before sampling — re-apply + re-sample is idempotent
        # (stable_freq_arrays only mutates after the per-chip evaluation
        # below), so a crash anywhere before then resumes cleanly.
        try:
            engine._save_checkpoint()
        except Exception as e:
            engine.log(f"Checkpoint save failed (non-fatal): {e}")

        health_scores = collect_chip_health_samples(
            engine, num_samples, interval, f"Round {engine.profiling_round}"
        )
        if isinstance(health_scores, dict) and health_scores.get("thermal_retry"):
            engine.log(
                f"Phase 3 round {engine.profiling_round}: thermal retry "
                f"— re-stabilize and re-sample (chip_max updated to "
                f"prevent re-attempting the hot freq)"
            )
            # Re-decrement profiling_round so the next iteration gets the
            # same round number (we didn't get a usable sample window).
            engine.profiling_round -= 1
            continue
        if isinstance(health_scores, dict) and health_scores.get("restart_round"):
            engine.log(
                f"Phase 3 round {engine.profiling_round}: miner stopped "
                f"hashing during sample collection — restarting round "
                f"(re-apply freqs, full STABILIZE_WAIT, fresh samples)"
            )
            engine.profiling_round -= 1
            continue
        if not engine.running or health_scores is None:
            break

        # Per-chip move decision. Dead chips are skipped entirely.
        # Each move is gated by the F clamp + relative spread cap; the
        # spread check uses the live stable_freq_arrays (which mutates as
        # earlier chips in the loop apply their moves), so chips later in
        # the iteration see the post-each-prior-move state.
        chips_moved = 0
        total_alive = 0
        for b in range(engine.num_boards):
            n = len(engine.stable_freq_arrays[b])
            if not n:
                continue
            alive_idx = [j for j in range(n) if j not in engine.parked_chips[b]]
            for i in range(n):
                if i in engine.parked_chips[b]:
                    continue
                if i >= len(health_scores[b]) or i >= len(engine.baseline_scores[b]):
                    continue
                total_alive += 1
                score = health_scores[b][i]
                baseline = engine.baseline_scores[b][i]
                cur = engine.stable_freq_arrays[b][i]
                if score >= baseline - up_tol:
                    # Stable -> try step UP. chip_max gates this: once the
                    # chip was found unstable at any freq X, all future UP
                    # attempts must satisfy `target < X`. Prevents the
                    # per-chip oscillation hazard where a chip steps
                    # UP -> unstable -> DOWN -> stable -> UP to the same
                    # unstable freq, repeating forever.
                    target = round((cur + step) / grid) * grid
                    cap = engine.chip_max[b][i]
                    if target > f_max_eff or target <= cur:
                        continue
                    if cap is not None and target >= cap:
                        continue
                    candidate = [
                        target if j == i else engine.stable_freq_arrays[b][j] for j in alive_idx
                    ]
                    if max(candidate) - min(candidate) > spread_cfg:
                        continue
                    engine.stable_freq_arrays[b][i] = target
                    chips_moved += 1
                elif score < baseline - down_tol:
                    # Unstable -> record this freq as the new lowest
                    # known-unstable (chip_max monotonically decreases),
                    # then try step DOWN.
                    if engine.chip_max[b][i] is None or cur < engine.chip_max[b][i]:
                        engine.chip_max[b][i] = cur
                    target = round((cur - step) / grid) * grid
                    if target < f_min_eff or target >= cur:
                        continue
                    candidate = [
                        target if j == i else engine.stable_freq_arrays[b][j] for j in alive_idx
                    ]
                    if max(candidate) - min(candidate) > spread_cfg:
                        continue
                    engine.stable_freq_arrays[b][i] = target
                    chips_moved += 1
                # else: in hold zone (between baseline-DOWN_TOL and
                # baseline-UP_TOL), no move.

        # Convergence-based progress: a chip has converged when it can no
        # longer step UP. Two cases: (a) chip_max is set and the next UP
        # would meet/exceed it (stability boundary bracketed), or (b) the
        # next UP target would exceed f_max_eff (chip is pinned to the F
        # grid ceiling and stable there). Both mean the chip's max stable
        # freq is known under current bounds. Monotonic within a tune
        # because chip_max only narrows and the grid ceiling is fixed.
        chips_converged = 0
        for b in range(engine.num_boards):
            for i in range(len(engine.stable_freq_arrays[b])):
                if i in engine.parked_chips[b]:
                    continue
                cur = engine.stable_freq_arrays[b][i]
                target = round((cur + step) / grid) * grid
                grid_blocked = target > f_max_eff
                cap = (
                    engine.chip_max[b][i]
                    if b < len(engine.chip_max) and i < len(engine.chip_max[b])
                    else None
                )
                cap_blocked = cap is not None and target >= cap
                if grid_blocked or cap_blocked:
                    chips_converged += 1
        engine.chips_converged = chips_converged
        engine.chips_alive = total_alive
        pct = 100.0 * chips_converged / max(1, total_alive)
        engine.profiling_completion_pct = pct
        engine.chips_stable_pct = pct

        # Per-board summary — current spread excludes dead chips so a
        # parked DEAD_CHIP_FREQ doesn't pollute the metric.
        for b in range(engine.num_boards):
            n = len(engine.stable_freq_arrays[b])
            if not n:
                continue
            alive_freqs = [
                engine.stable_freq_arrays[b][i] for i in range(n) if i not in engine.parked_chips[b]
            ]
            if not alive_freqs:
                continue
            avg = statistics.mean(alive_freqs)
            cur_spread = max(alive_freqs) - min(alive_freqs)
            dead_cnt = len(engine.parked_chips[b])
            dead_str = f", {dead_cnt} dead" if dead_cnt else ""
            engine.log(
                f"Round {engine.profiling_round} Board {b}: "
                f"avg={avg:.1f} MHz, spread={cur_spread:.1f}/{spread_cfg} MHz"
                f"{dead_str}"
            )
        engine.log(
            f"Round {engine.profiling_round}: "
            f"{chips_converged}/{total_alive} chips converged "
            f"({chips_moved} moves this round)"
        )

        try:
            engine._save_checkpoint()
        except Exception as e:
            engine.log(f"Checkpoint save failed (non-fatal): {e}")

        # Stillness streak: a single zero-move round could be sample noise.
        # Require N consecutive zero-move rounds before declaring done.
        if chips_moved == 0:
            engine.stillness_streak += 1
            if engine.stillness_streak >= streak_target:
                engine.log(
                    f"Phase 3 complete: {engine.stillness_streak} consecutive "
                    f"zero-move rounds at round {engine.profiling_round}"
                )
                break
        else:
            engine.stillness_streak = 0

    if engine.profiling_round >= max_rounds and engine.running:
        engine.log(
            f"Phase 3 hit MAX_PROFILING_ROUNDS={max_rounds} without "
            f"stillness convergence — using current per-chip freqs"
        )

    # Final apply (stable_freq_arrays already up to date; this just makes
    # sure the firmware matches our state going into Phase 3b/Phase 4).
    if engine.running:
        engine._apply_stable_freqs()
        engine._wait_for_mining_state(timeout=600)

    engine.phase3_active = False


def phase3b_polish(engine):
    """Decrement-only stability polish after Phase 3's iterative loop.

    Phase 3 terminates on per-round health snapshots (ROUND_SAMPLES samples
    at ROUND_INTERVAL spacing) which can miss slow drift. Phase 3b uses a
    longer dedicated sample window (STABILITY_POLISH_ROUND_SAMPLES /
    STABILITY_POLISH_ROUND_INTERVAL) and drops any chip whose averaged
    health falls below baseline - CHIP_TUNE_DOWN_TOLERANCE by one polish
    step (snapped to the 3.125 grid). Never raises any chip's frequency;
    a round with zero changes exits the loop early.

    Resume-safe: polish_round / polish_active round-trip through the
    checkpoint, and a crash mid-polish re-applies stable_freq_arrays and
    picks up at the saved round.
    """
    rounds_cfg = int(engine.config.get("STABILITY_POLISH_ROUNDS", 3))
    if rounds_cfg <= 0:
        engine.log("Phase 3b: polish disabled (STABILITY_POLISH_ROUNDS=0)")
        return
    grid = 3.125
    step = float(engine.config.get("STABILITY_POLISH_STEP_MHZ", 6.25))
    step = max(grid, round(step / grid) * grid)
    # Use the iterative loop's down-tolerance — Phase 3b only decrements,
    # so the up-tolerance side doesn't apply here.
    down_tol = float(engine.config["CHIP_TUNE_DOWN_TOLERANCE"])
    spread_cfg = int(engine.config["CHIP_FREQ_SPREAD_MHZ"])
    f_min_eff = max(
        round(float(engine.config["VF_EXPLORE_F_MIN"]) / grid) * grid,
        float(FIRMWARE_FREQ_MIN_MHZ),
    )

    engine.phase = engine.PHASE_POLISH
    engine.polish_active = True

    # MRR sync at polish entry: when MRR_PUBLISH_DURING_POLISH is enabled, fire
    # mrr_sync("maintaining") ONCE per polish phase entry per chip-tune voltage.
    # Fire ONCE at FRESH polish start (polish_round == 0), NOT after each polish
    # round — polish decrements frequencies and re-firing would over-advertise
    # progressively. The polish_round == 0 guard also prevents firing on a
    # mid-polish resume from checkpoint (where _mrr_polish_announced is reset
    # by __init__ but polish_round survives in the loaded state). The dedup
    # flag _mrr_polish_announced is cleared at chip-tune voltage start in
    # chip_tune_orchestration.py and on lifecycle events.
    if (
        engine.config.get("MRR_PUBLISH_DURING_POLISH", False)
        and not engine._mrr_polish_announced
        and engine.polish_round == 0
    ):
        engine._mrr_sync("maintaining", reason="Entered Phase 3b polish")
        engine._mrr_polish_announced = True

    # Polish-specific (longer) sampling window — catches slow drift the
    # shorter Phase 3 ROUND_SAMPLES missed.  Read before the resuming
    # branch so stabilize_time is available for the startup log.
    num_samples = int(engine.config.get("STABILITY_POLISH_ROUND_SAMPLES", 40))
    interval = int(engine.config.get("STABILITY_POLISH_ROUND_INTERVAL", 30))
    stabilize_time = int(
        engine.config.get("STABILITY_POLISH_STABILIZE_WAIT", engine.config["STABILIZE_WAIT"])
    )

    resuming = engine.polish_round > 0
    if resuming:
        engine.log(f"Phase 3b: Resuming stability polish from round {engine.polish_round}")
    else:
        engine.polish_round = 0
        engine.log(
            f"Phase 3b: Stability polish — up to {rounds_cfg} rounds, "
            f"step {step:.3f} MHz (decrement-only), DOWN_TOL={down_tol:.0f}, "
            f"stabilize_wait={stabilize_time}s, "
            f"F_MIN={f_min_eff:.0f} MHz, "
            f"max inter-chip spread {spread_cfg} MHz"
        )

    while engine.running and engine.polish_round < rounds_cfg:
        engine.polish_round += 1
        engine.phase_detail = f"Polish round {engine.polish_round}: applying freqs"

        # Re-apply current stable_freq_arrays so any resume lands on the
        # right clocks (firmware may have reset between a crash and now).
        if engine.running:
            engine._apply_stable_freqs()
            engine._wait_for_mining_state(timeout=600)

        # Thermal stabilization before sampling, same pattern as Phase 3.
        remaining = stabilize_time
        while remaining > 0 and engine.running:
            engine.phase_detail = (
                f"Polish round {engine.polish_round}: stabilizing ({remaining}s remaining)"
            )
            time.sleep(min(remaining, 10))
            remaining -= 10
            engine._update_live_data()
            em = engine._detect_thermal_emergency()
            if em:
                engine._handle_thermal_in_chip_tune(em)
        if not engine.running:
            break

        # Checkpoint before sampling — re-apply + re-sample is idempotent
        # for this round (changes only land below).
        try:
            engine._save_checkpoint()
        except Exception as e:
            engine.log(f"Checkpoint save failed (non-fatal): {e}")

        health_scores = collect_chip_health_samples(
            engine, num_samples, interval, f"Polish round {engine.polish_round}"
        )
        if isinstance(health_scores, dict) and health_scores.get("thermal_retry"):
            engine.log(
                f"Phase 3b polish round {engine.polish_round}: thermal "
                f"retry — re-stabilize and re-sample"
            )
            # Decrement so the next iteration gets the same round number.
            engine.polish_round -= 1
            continue
        if isinstance(health_scores, dict) and health_scores.get("restart_round"):
            engine.log(
                f"Phase 3b polish round {engine.polish_round}: miner "
                f"stopped hashing during sample collection — restarting "
                f"round (re-apply freqs, {stabilize_time}s STABILITY_POLISH_STABILIZE_WAIT "
                f"stabilize, fresh samples)"
            )
            engine.polish_round -= 1
            continue
        if not engine.running or health_scores is None:
            break

        # Decrement any chip whose health dropped below baseline by more
        # than DOWN_TOL. Parked (dead) chips are skipped. Each decrement
        # is gated by the F_MIN clamp + relative spread cap (a chip
        # already at the cohort minimum can't decrement further without
        # widening the spread past the cap; it stays put and surfaces in
        # the log so the operator can decide whether to widen SPREAD or
        # accept it as a parking candidate).
        any_changed = False
        per_board_drops = [0] * engine.num_boards
        for b in range(engine.num_boards):
            n = len(engine.stable_freq_arrays[b])
            if not n:
                continue
            alive_idx = [j for j in range(n) if j not in engine.parked_chips[b]]
            for i in range(n):
                if i in engine.parked_chips[b]:
                    continue
                if i >= len(health_scores[b]) or i >= len(engine.baseline_scores[b]):
                    continue
                score = health_scores[b][i]
                baseline = engine.baseline_scores[b][i]
                if score >= baseline - down_tol:
                    continue
                cur = engine.stable_freq_arrays[b][i]
                new_freq = round(max(f_min_eff, cur - step) / grid) * grid
                if new_freq >= cur:
                    continue  # at floor, or step rounded to no-op
                candidate = [
                    new_freq if j == i else engine.stable_freq_arrays[b][j] for j in alive_idx
                ]
                new_spread = max(candidate) - min(candidate)
                if new_spread > spread_cfg:
                    engine.log(
                        f"Phase 3b: chip ({b},{i}) decrement blocked by "
                        f"spread cap (score={score:.0f}, would widen to "
                        f"{new_spread:.0f}/{spread_cfg} MHz)"
                    )
                    continue
                engine.stable_freq_arrays[b][i] = new_freq
                any_changed = True
                per_board_drops[b] += 1

        total_drops = sum(per_board_drops)
        engine.log(
            f"Polish round {engine.polish_round}/{rounds_cfg}: "
            f"{total_drops} chips dropped "
            f"(per-board: {per_board_drops})"
        )

        try:
            engine._save_checkpoint()
        except Exception as e:
            engine.log(f"Checkpoint save failed (non-fatal): {e}")

        if not any_changed:
            engine.log(
                f"Phase 3b: all chips stable at current freqs "
                f"(round {engine.polish_round}/{rounds_cfg}) — polish complete"
            )
            break

    # Apply the final polished freqs so Phase 4 measures against them.
    if engine.running:
        engine._apply_stable_freqs()
        engine._wait_for_mining_state(timeout=600)

    engine.polish_active = False
