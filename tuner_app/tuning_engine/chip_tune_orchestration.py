"""
Chip tuning orchestration functions for the TuningEngine.
These are free functions extracted from the TuningEngine class methods,
with the first parameter renamed from `self` to `engine` and all `self.` references
converted to `engine.` to enable standalone usage.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime

from tuner_app.tuning_engine.grid import vf_surface_by_key


def populate_fine_tune_fields(engine, result, voltage_mv, seed_f_mhz):
    """Populate fine_tune_* fields on a voltage_results entry from the matching
    Phase V (uniform-frequency) surface measurement at (voltage_mv, seed_f_mhz).

    The fields enable evaluate_chip_tune_fallback (monitor.py) to flip between
    chip-tuned per-chip arrays and a uniform-freq fallback at the same voltage
    when current scoring favors the uniform variant. Without this, every cell's
    fine_tune_* would be None and the fallback would early-return.

    vf_surface entries don't store stable_freq_arrays — Phase V's
    measure_vf_point applies seed_f_mhz uniformly across alive chips with
    parked chips pinned at DEAD_CHIP_FREQ. Reconstruct that array shape from
    the engine's current parked_chips mask so a fallback flip applies the
    same per-chip pattern Phase V actually measured.

    No-op when the matching vf_surface cell is missing or has no efficiency
    data — fields are set to None so old-shape consumers see the documented
    sentinel.
    """
    _vf_key = (int(voltage_mv), round(float(seed_f_mhz), 3))
    _vf_entry = vf_surface_by_key(engine).get(_vf_key)
    if _vf_entry is None or _vf_entry.get("efficiency_jth") is None:
        result["fine_tune_freq_arrays"] = None
        result["fine_tune_efficiency_jth"] = None
        result["fine_tune_hashrate_ths"] = None
        result["fine_tune_power_w"] = None
        return
    dead_freq = float(engine.config["DEAD_CHIP_FREQ"])
    seed = float(seed_f_mhz)
    fine_arrays = []
    for b in range(engine.num_boards):
        if engine.stable_freq_arrays[b]:
            n = len(engine.stable_freq_arrays[b])
        else:
            n = engine.chips_per_board
        row = [dead_freq if i in engine.parked_chips[b] else seed for i in range(n)]
        fine_arrays.append(row)
    result["fine_tune_freq_arrays"] = fine_arrays
    result["fine_tune_efficiency_jth"] = _vf_entry.get("efficiency_jth")
    result["fine_tune_hashrate_ths"] = _vf_entry.get("hashrate_ths")
    result["fine_tune_power_w"] = _vf_entry.get("power_w")


def do_chip_tune_atomic(engine, target, fresh_start=True):
    """Atomic chip-tune: Phase 3 iterative loop + Phase 3b polish + Phase 4
    measure on one cell. Appends to voltage_results.

    "Atomic" means: settings changes mid-cell don't change the target,
    the dynamic loop won't re-evaluate find_next_* between phases. BUT:
    operator stop (engine.running = False) DOES preempt — Phase 3's sample
    loops check engine.running. On stop, the cell's voltage_results entry
    is not appended; the next loop entry will resume via
    in_flight_chip_tune_target with fresh_start=False (preserving
    stable_freq_arrays / profiling_round / stillness_streak).

    Resume safety: when fresh_start=False (the resume branch), the retry
    loop's _reset_to_safe_vf may have just put the miner at
    BASELINE_VOLTAGE_MV (~15V). _run_phase3_phase4_at_voltage's
    fresh_start=True path re-applies the target voltage via
    _phase1_set_voltage; the fresh_start=False path skips it. So we
    explicitly re-apply the target voltage here on the resume path
    before delegating, AND we re-seed stable_freq_arrays around the
    target if the prior crash happened before Phase 3's first round
    checkpoint had a chance to save them, OR if the saved state is
    incoherent under the current Phase 3 contracts (per-board alive
    spread > CHIP_FREQ_SPREAD_MHZ, or any alive chip outside
    [VF_EXPLORE_F_MIN, VF_EXPLORE_F_MAX]). Either condition is a sign
    of corrupt or stale state from a different tune at different
    bounds."""
    voltage_mv = int(target["voltage_mv"])
    seed_f_mhz = float(target["freq_mhz"])
    vf_source = target.get("vf_source")
    if not fresh_start:
        # Re-apply target V/F so a recovery's _reset_to_safe_vf doesn't
        # leave Phase 3 measuring at BASELINE_VOLTAGE_MV instead of the
        # cell's actual voltage. _phase1_set_voltage handles V+F ordering.
        try:
            cur_v = engine._get_current_voltage_mv()
        except Exception:
            cur_v = 0
        if cur_v == 0 or abs(cur_v - voltage_mv) > 100:
            engine.log(
                f"Chip-tune resume: re-applying voltage {voltage_mv} mV (current {cur_v} mV)"
            )
            engine._phase1_set_voltage(voltage_mv, seed_f_mhz)
            if not engine.running:
                return None
        # Resume seed defense — stable_freq_arrays IS the iterative loop's
        # state. Under the relative-spread + F-clamp design, legitimate
        # cohort drift can carry chips arbitrarily far from seed_f, so the
        # corruption check is bounds-coherence rather than distance from
        # seed_f: reseed if state is missing, OR per-board alive spread
        # exceeds CHIP_FREQ_SPREAD_MHZ, OR any alive chip is outside
        # [VF_EXPLORE_F_MIN, VF_EXPLORE_F_MAX]. Either condition means
        # the saved state is incoherent for the current config — possibly
        # corrupt, possibly stale from a different tune at different
        # bounds — and reseeding to seed_f is the safe restart.
        spread = int(engine.config["CHIP_FREQ_SPREAD_MHZ"])
        f_min_cfg = float(engine.config["VF_EXPLORE_F_MIN"])
        f_max_cfg = float(engine.config["VF_EXPLORE_F_MAX"])
        grid = 3.125
        need_reseed = not engine.stable_freq_arrays or not any(engine.stable_freq_arrays)
        if not need_reseed:
            for b in range(engine.num_boards):
                if need_reseed:
                    break
                alive = [
                    engine.stable_freq_arrays[b][i]
                    for i in range(len(engine.stable_freq_arrays[b]))
                    if i not in engine.parked_chips[b]
                ]
                if not alive:
                    continue
                if max(alive) - min(alive) > spread:
                    need_reseed = True
                    break
                if min(alive) < f_min_cfg or max(alive) > f_max_cfg:
                    need_reseed = True
                    break
        if need_reseed:
            engine.log(
                f"Chip-tune resume: re-seeding stable_freq_arrays "
                f"around target seed_f={seed_f_mhz:.1f} MHz "
                f"(SPREAD={spread} MHz window)"
            )
            engine._phase1_set_voltage(voltage_mv, seed_f_mhz)
            if not engine.running:
                return None
            clocks_data = engine.api.clocks()
            if clocks_data:
                engine.stable_freq_arrays = [[] for _ in range(engine.num_boards)]
                engine.proposed_freqs = [[] for _ in range(engine.num_boards)]
                for board in clocks_data:
                    idx = board.index
                    if idx < engine.num_boards:
                        engine.stable_freq_arrays[idx] = list(board.chip_freqs_mhz)
                        engine.proposed_freqs[idx] = list(board.chip_freqs_mhz)
            engine.parked_chips = [set() for _ in range(engine.num_boards)]
            engine._park_dead_chips_from_baseline()
            seed_snap = round(float(seed_f_mhz) / grid) * grid
            for b in range(engine.num_boards):
                for i in range(len(engine.stable_freq_arrays[b])):
                    if i in engine.parked_chips[b]:
                        continue
                    engine.stable_freq_arrays[b][i] = seed_snap
            # Reset round counters too — we're effectively starting fresh.
            engine.profiling_round = 0
            engine.stillness_streak = 0
            engine.chip_max = None  # Stale memory from a different tune; rebuild from scratch.
            engine.phase3_active = False
    return run_phase3_phase4_at_voltage(
        engine, voltage_mv, seed_f_mhz, fresh_start=fresh_start, vf_source=vf_source
    )


def run_phase3_phase4_at_voltage(engine, voltage_mv, seed_f_mhz, fresh_start=True, vf_source=None):
    """Run Phase 3 (iterative per-chip health tune) + Phase 3b polish +
    Phase 4 (measure J/TH) at `voltage_mv`, seeded by the uniform-F winner
    from Phase V. Appends one entry to voltage_results and returns it, or
    None on measurement failure.

    fresh_start=False preserves in-flight Phase 3 state (stable_freq_arrays,
    profiling_round, stillness_streak, parked_chips) so a crash mid-Phase-3
    resumes at the saved round instead of restarting.

    vf_source (optional) is the originating (V, F) surface cell descriptor
    from Phase V's top-K selection — R6 persists it on the voltage_results
    entry so the dashboard cell popup can show before/after chip-tune."""
    grid = 3.125

    engine.current_sweep_voltage_mv = voltage_mv
    engine.min_voltage_mv = voltage_mv
    if engine.current_step_started_at is None:
        engine.current_step_started_at = time.time()
    engine.profiling_completion_pct = 0.0
    engine.chips_stable_pct = 0.0
    engine.chips_converged = 0
    engine.chips_alive = 0

    # Bixbit: skip iterative Phase 3 + Phase 3b (firmware auto-tunes
    # per-chip internally). Apply V+F then go straight to Phase 4
    # efficiency measurement. stable_freq_arrays are empty (no per-chip
    # state on Bixbit). voltage_results entry gets chip_tune_active=False.
    if not engine.api.supports_per_chip_tuning():
        engine._phase1_set_voltage(voltage_mv, seed_f_mhz)
        if not engine.running:
            return None
        # Empty per-board arrays: Bixbit has no per-chip freq state.
        engine.stable_freq_arrays = [[] for _ in range(engine.num_boards)]
        engine.proposed_freqs = [[] for _ in range(engine.num_boards)]

        efficiency = phase4_measure_efficiency(engine)
        if not engine.running:
            return None
        if isinstance(efficiency, dict) and efficiency.get("thermal_failed"):
            if vf_source:
                src_v = int(vf_source.get("voltage_mv", -1))
                src_f = float(vf_source.get("freq_mhz", -1))
                tol = float(engine.config.get("FREQ_SEARCH_TOLERANCE_MHZ", 7))
                for entry in engine.vf_surface:
                    if int(entry.get("voltage_mv", -1)) != src_v:
                        continue
                    if abs(float(entry.get("freq_mhz", 0)) - src_f) > tol:
                        continue
                    entry["thermal_failed"] = True
                    entry["measured_at"] = datetime.now().isoformat()
                    engine.log(
                        f"Phase 4 thermal_failed (no per-chip tuning): "
                        f"marked vf_surface cell ({src_v} mV, {src_f:.1f} MHz)"
                    )
                    break
            engine.current_sweep_voltage_mv = None
            engine.current_step_started_at = None
            try:
                engine._save_checkpoint()
            except Exception as ex:
                engine.log(
                    f"Phase 4 thermal_failed (no per-chip tuning): checkpoint save failed: {ex}"
                )
            return None
        if efficiency is None:
            engine.log(
                f"Phase 4 (no per-chip tuning): could not measure efficiency at {voltage_mv} mV"
            )
            engine.current_sweep_voltage_mv = None
            engine.current_step_started_at = None
            return None

        duration_sec = time.time() - (engine.current_step_started_at or time.time())
        engine.current_step_started_at = None

        result = {
            "voltage_mv": voltage_mv,
            "efficiency_jth": efficiency["efficiency_jth"],
            "hashrate_ths": efficiency["hashrate_ths"],
            "power_w": efficiency["power_w"],
            "avg_freq_mhz": float(seed_f_mhz),
            "duration_sec": duration_sec,
            "per_board": efficiency.get("per_board", []),
            "measured_at": datetime.now().isoformat(),
            # Empty per-board arrays — Bixbit has no per-chip freq state.
            "stable_freq_arrays": [[] for _ in range(engine.num_boards)],
            "baseline_scores": [[] for _ in range(engine.num_boards)],
            "from_vf_exploration": True,
            "seed_f_mhz": float(seed_f_mhz),
            "vf_source": dict(vf_source) if vf_source else None,
            "fine_tune_freq_arrays": None,
            "fine_tune_efficiency_jth": None,
            "fine_tune_hashrate_ths": None,
            "fine_tune_power_w": None,
            "chip_tune_active": False,
            "last_fallback_decision_ts": None,
            "last_fallback_score_delta": None,
        }
        replaced = False
        for i, prior in enumerate(engine.voltage_results):
            if prior.get("voltage_mv") == voltage_mv:
                if result.get("vf_source") is None and prior.get("vf_source"):
                    result["vf_source"] = prior.get("vf_source")
                engine.voltage_results[i] = result
                replaced = True
                break
        if not replaced:
            engine.voltage_results.append(result)
        if engine.best_efficiency is None or result["efficiency_jth"] < engine.best_efficiency:
            engine.best_efficiency = result["efficiency_jth"]
        engine.log(
            f"Bixbit top-K at {voltage_mv} mV: "
            f"{efficiency['hashrate_ths']:.2f} TH/s, {efficiency['power_w']:.0f} W, "
            f"{efficiency['efficiency_jth']:.2f} J/TH (seed freq {seed_f_mhz:.0f} MHz)"
        )
        engine.current_sweep_voltage_mv = None
        engine._save_checkpoint()
        return result

    if fresh_start:
        engine._phase1_set_voltage(voltage_mv, seed_f_mhz)
        if not engine.running:
            return None
        engine.parked_chips = [set() for _ in range(engine.num_boards)]
        # Snapshot the firmware's current per-chip clocks (which Phase 1
        # just set uniformly to seed_f via set_clock_all). This is the
        # initial stable_freq_arrays for the iterative loop.
        clocks_data = engine.api.clocks()
        if clocks_data:
            engine.stable_freq_arrays = [[] for _ in range(engine.num_boards)]
            engine.proposed_freqs = [[] for _ in range(engine.num_boards)]
            for board in clocks_data:
                idx = board.index
                if idx < engine.num_boards:
                    engine.stable_freq_arrays[idx] = list(board.chip_freqs_mhz)
                    engine.proposed_freqs[idx] = list(board.chip_freqs_mhz)
        # Park dead chips AFTER snapshotting clocks so their pinned
        # DEAD_CHIP_FREQ overrides whatever the firmware reported.
        engine._park_dead_chips_from_baseline()
        # Snap alive chips to seed_f on the 3.125 MHz grid so the loop's
        # first round operates on a consistent starting frequency rather
        # than the raw firmware-reported value (which may be off-grid by
        # rounding artifacts).
        seed_snap = round(float(seed_f_mhz) / grid) * grid
        for b in range(engine.num_boards):
            for i in range(len(engine.stable_freq_arrays[b])):
                if i in engine.parked_chips[b]:
                    continue
                engine.stable_freq_arrays[b][i] = seed_snap
        # Fresh start of this top-K iteration — Phase 3 + 3b state reset.
        engine.profiling_round = 0
        engine.stillness_streak = 0
        engine.chip_max = None  # Cleared so _phase3_profiling re-initializes for the new voltage's stability profile.  # noqa: E501
        engine.phase3_active = False
        engine.polish_round = 0
        engine.polish_active = False
        engine._mrr_polish_announced = False

    # Phase 3 — skip if we already entered the polish phase in a prior
    # process life (polish_round > 0 or polish_active). Otherwise run
    # the iterative loop (which itself resumes from checkpointed state
    # when phase3_active was true).
    polish_started = engine.polish_active or engine.polish_round > 0
    if not polish_started:
        engine._phase3_profiling(seed_f_mhz)
        if not engine.running:
            return None

    # Phase 3b — grid-step stability polish. No-op when disabled
    # (STABILITY_POLISH_ROUNDS=0) or already past its configured round
    # count on a resume.
    engine._phase3b_polish()
    if not engine.running:
        return None

    efficiency = phase4_measure_efficiency(engine)
    if not engine.running:
        return None
    if isinstance(efficiency, dict) and efficiency.get("thermal_failed"):
        # Phase 4 hit a thermal emergency. Mark the originating vf_surface
        # cell as thermal_failed so the next find_next_chip_tune_target
        # won't pick it again (score_cell returns None for thermal_failed).
        # The operator can re-queue the cell via remeasure once cooling
        # improves.
        if vf_source:
            src_v = int(vf_source.get("voltage_mv", -1))
            src_f = float(vf_source.get("freq_mhz", -1))
            tol = float(engine.config.get("FREQ_SEARCH_TOLERANCE_MHZ", 7))
            for entry in engine.vf_surface:
                if int(entry.get("voltage_mv", -1)) != src_v:
                    continue
                if abs(float(entry.get("freq_mhz", 0)) - src_f) > tol:
                    continue
                entry["thermal_failed"] = True
                entry["measured_at"] = datetime.now().isoformat()
                engine.log(
                    f"Phase 4 thermal_failed: marked vf_surface cell "
                    f"({src_v} mV, {src_f:.1f} MHz) thermal_failed"
                )
                break
        engine.current_sweep_voltage_mv = None
        engine.current_step_started_at = None
        try:
            engine._save_checkpoint()
        except Exception as ex:
            engine.log(f"Phase 4 thermal_failed: checkpoint save failed: {ex}")
        return None
    if efficiency is None:
        engine.log(f"Could not measure efficiency at {voltage_mv} mV")
        engine.current_sweep_voltage_mv = None
        engine.current_step_started_at = None
        return None

    avg_freq = 0.0
    total_chips = 0
    for b in range(engine.num_boards):
        if engine.stable_freq_arrays[b]:
            avg_freq += sum(engine.stable_freq_arrays[b])
            total_chips += len(engine.stable_freq_arrays[b])
    if total_chips > 0:
        avg_freq /= total_chips

    duration_sec = time.time() - (engine.current_step_started_at or time.time())
    engine.current_step_started_at = None

    result = {
        "voltage_mv": voltage_mv,
        "efficiency_jth": efficiency["efficiency_jth"],
        "hashrate_ths": efficiency["hashrate_ths"],
        "power_w": efficiency["power_w"],
        "avg_freq_mhz": avg_freq,
        "duration_sec": duration_sec,
        "per_board": efficiency.get("per_board", []),
        "measured_at": datetime.now().isoformat(),
        "stable_freq_arrays": [arr[:] for arr in engine.stable_freq_arrays],
        "baseline_scores": [arr[:] for arr in engine.baseline_scores],
        "from_vf_exploration": True,
        "seed_f_mhz": float(seed_f_mhz),
        "vf_source": dict(vf_source) if vf_source else None,
    }
    populate_fine_tune_fields(engine, result, voltage_mv, seed_f_mhz)
    result["chip_tune_active"] = True
    result["last_fallback_decision_ts"] = None
    result["last_fallback_score_delta"] = None
    # Replace any prior entry for this voltage — resume safety guards
    # against a half-finished measurement stacking on top of the new one.
    replaced = False
    for i, prior in enumerate(engine.voltage_results):
        if prior.get("voltage_mv") == voltage_mv:
            # Preserve existing vf_source when caller didn't supply one
            # (e.g. retune path predates R6's vf_source stamp).
            if result.get("vf_source") is None and prior.get("vf_source"):
                result["vf_source"] = prior.get("vf_source")
            engine.voltage_results[i] = result
            replaced = True
            break
    if not replaced:
        engine.voltage_results.append(result)
    if engine.best_efficiency is None or result["efficiency_jth"] < engine.best_efficiency:
        engine.best_efficiency = result["efficiency_jth"]
    engine.log(
        f"Top-K tune at {voltage_mv} mV: "
        f"{efficiency['hashrate_ths']:.2f} TH/s, {efficiency['power_w']:.0f} W, "
        f"{efficiency['efficiency_jth']:.2f} J/TH (avg freq {avg_freq:.0f} MHz)"
    )
    engine.current_sweep_voltage_mv = None
    engine._save_checkpoint()
    return result


def phase4_measure_efficiency(engine):
    """Let the miner run with tuned frequencies and measure actual J/TH.

    Returns None on operator stop or non-thermal measurement failure.
    Returns the sentinel `{"thermal_failed": True}` if a thermal emergency
    fired during the wait or sampling — the caller (`_run_phase3_phase4_at_voltage`)
    propagates this up so the source vf_surface cell can be marked
    thermal_failed. On success, returns the standard efficiency dict."""
    engine.phase = engine.PHASE_MEASURE
    wait_time = engine.config["EFFICIENCY_MEASURE_WAIT"]
    engine.phase_detail = f"Measuring efficiency (waiting {wait_time}s for stable reading)"
    engine.log(
        f"Phase 4: Measuring efficiency at {engine.min_voltage_mv} mV (waiting {wait_time}s)"
    )

    engine._apply_stable_freqs()
    engine._wait_for_mining_state(timeout=600)

    remaining = wait_time
    while remaining > 0 and engine.running:
        time.sleep(min(remaining, 10))
        remaining -= 10
        engine._update_live_data()
        em = engine._detect_thermal_emergency()
        if em:
            engine._handle_thermal_in_vf_measure(
                em,
                engine.min_voltage_mv,
                float(engine.stable_freq_arrays[0][0])
                if engine.stable_freq_arrays and engine.stable_freq_arrays[0]
                else 0.0,
                fine=False,
            )
            return {"thermal_failed": True}

    if not engine.running:
        return None

    readings = []
    per_board_acc = [
        {"hashrate_mhs": [], "board_temp": [], "clock": [], "health": [], "input_voltage": []}
        for _ in range(engine.num_boards)
    ]
    temps_snapshot = engine.api.temps()
    temps_by_idx = {b.index: b for b in temps_snapshot}
    for _ in range(5):
        em = engine._detect_thermal_emergency()
        if em:
            engine._handle_thermal_in_vf_measure(
                em,
                engine.min_voltage_mv,
                float(engine.stable_freq_arrays[0][0])
                if engine.stable_freq_arrays and engine.stable_freq_arrays[0]
                else 0.0,
                fine=False,
            )
            return {"thermal_failed": True}
        summary = engine.api.summary()
        if summary:
            power_w = summary.power_w
            ths = summary.hashrate_ths
            if ths > 0 and power_w > 0:
                readings.append(
                    {"hashrate_ths": ths, "power_w": power_w, "efficiency_jth": power_w / ths}
                )
                for board in summary.boards:
                    idx = board.index
                    if 0 <= idx < engine.num_boards:
                        per_board_acc[idx]["hashrate_mhs"].append(board.hashrate_ths * 1e6)
                        board_temps = temps_by_idx.get(idx)
                        per_board_acc[idx]["board_temp"].append(
                            board_temps.temp_outlet_c
                            if board_temps and board_temps.temp_outlet_c is not None
                            else 0
                        )
                        per_board_acc[idx]["clock"].append(board.freq_mhz or 0)
                        per_board_acc[idx]["health"].append(board.board_health_pct or 0)
                        per_board_acc[idx]["input_voltage"].append(board.target_voltage_mv or 0)
        time.sleep(5)

    if not readings:
        return None

    # Chip temps + inlet/outlet board temps — sampled once, since they
    # move slowly relative to hashrate.
    chip_temps = [[] for _ in range(engine.num_boards)]
    try:
        for entry in engine.api.temps_chip() or []:
            idx = entry.index
            if 0 <= idx < engine.num_boards:
                chip_temps[idx] = list(entry.chip_temps_c)
    except Exception as ex:
        engine.log(f"Warn: failed to sample chip temps for per-board report: {ex}")

    inlet_outlet = [{"inlet": None, "outlet": None} for _ in range(engine.num_boards)]
    try:
        for entry in engine.api.temps() or []:
            idx = entry.index
            if 0 <= idx < engine.num_boards:
                inlet = entry.temp_inlet_c
                outlet = entry.temp_outlet_c
                if inlet is not None and outlet is not None:
                    inlet_outlet[idx] = {"inlet": inlet, "outlet": outlet}
                elif inlet is not None:
                    inlet_outlet[idx] = {"inlet": inlet, "outlet": inlet}
    except Exception as ex:
        engine.log(f"Warn: failed to sample board inlet/outlet temps: {ex}")

    def _safe_mean(xs):
        return statistics.mean(xs) if xs else 0

    per_board = []
    for i in range(engine.num_boards):
        acc = per_board_acc[i]
        chip_t = [t for t in chip_temps[i] if isinstance(t, (int, float)) and t > 0]
        tuned_clocks = engine.stable_freq_arrays[i] if i < len(engine.stable_freq_arrays) else []
        per_board.append(
            {
                "index": i,
                "hashrate_ths": _safe_mean(acc["hashrate_mhs"]) / 1_000_000,
                "avg_clock_mhz": _safe_mean(acc["clock"]),
                "tuned_avg_freq_mhz": (statistics.mean(tuned_clocks) if tuned_clocks else 0),
                "board_temp_c": _safe_mean(acc["board_temp"]),
                "inlet_temp_c": inlet_outlet[i]["inlet"],
                "outlet_temp_c": inlet_outlet[i]["outlet"],
                "chip_temp_min_c": min(chip_t) if chip_t else None,
                "chip_temp_avg_c": statistics.mean(chip_t) if chip_t else None,
                "chip_temp_max_c": max(chip_t) if chip_t else None,
                "health_pct": _safe_mean(acc["health"]),
                "input_voltage_v": _safe_mean(acc["input_voltage"]),
            }
        )

    avg = {
        "hashrate_ths": statistics.mean(r["hashrate_ths"] for r in readings),
        "power_w": statistics.mean(r["power_w"] for r in readings),
        "efficiency_jth": statistics.mean(r["efficiency_jth"] for r in readings),
        "per_board": per_board,
    }
    engine.log(
        f"Measured: {avg['hashrate_ths']:.1f} TH/s, {avg['power_w']:.0f}W, "
        f"{avg['efficiency_jth']:.2f} J/TH"
    )
    for b in per_board:
        if b["chip_temp_max_c"] is not None:
            engine.log(
                f"  Board {b['index']}: {b['hashrate_ths']:.1f} TH/s @ "
                f"{b['avg_clock_mhz']:.0f} MHz, board {b['board_temp_c']:.1f}C, "
                f"chips {b['chip_temp_min_c']:.0f}/{b['chip_temp_avg_c']:.0f}/"
                f"{b['chip_temp_max_c']:.0f}C (min/avg/max)"
            )
        else:
            engine.log(
                f"  Board {b['index']}: {b['hashrate_ths']:.1f} TH/s @ "
                f"{b['avg_clock_mhz']:.0f} MHz, board {b['board_temp_c']:.1f}C"
            )
    return avg
