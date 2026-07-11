"""
Free functions extracted from TuningEngine class for measuring voltage-frequency points.
These functions handle the core measurement logic for exploring the voltage-frequency surface
of mining hardware, including sampling, stability checks, and thermal emergency handling.
"""

from __future__ import annotations

import statistics
import time
from datetime import datetime

from tuner_app.miner.exceptions import MinerNotReady


def measure_vf_point(engine, v_mv, f_mhz, fine=False):
    """Apply (v_mv, f_mhz), wait VF_EXPLORE_WAIT, collect VF_EXPLORE_SAMPLES
    J/TH samples. Returns a dict conforming to the vf_surface[] schema.
    efficiency/hashrate/power are None only when the API returns nothing
    usable (fail-closed no-data, not counted as a trend point by the
    caller). Publishes self.current_vf_point for the dashboard so the
    in-flight cell can pulse.

    Phase 1 settle failure escape hatch: when _phase1_set_voltage (called
    from measure_vf_point_inner) raises MinerNotReady — typically because
    the PSU can't reach the commanded voltage within
    SETTLE_VOLTAGE_TOLERANCE_MV — append a thermal_failed sentinel to
    vf_surface for this cell BEFORE re-raising. Without the sentinel, the
    state machine's find_next_coarse_to_measure would re-pick the same
    unreachable cell on the next iteration (after recovery), and the
    outer retry loop would loop forever even with the confirmed-good
    timestamp fix in place (each iteration legitimately reaches Phase 0
    success before failing in Phase 1). The sentinel uses thermal_failed
    because score_cell already filters those out of ranking and trend
    walks. Operator can remeasure the cell explicitly via the remeasure
    queue if the PSU range improves.
    """
    wait = int(engine.config["VF_EXPLORE_WAIT"])
    n_samples = max(1, int(engine.config["VF_EXPLORE_SAMPLES"]))
    sample_interval = max(1, int(engine.config["VF_EXPLORE_SAMPLE_INTERVAL"]))

    engine.current_vf_point = {
        "voltage_mv": int(v_mv),
        "freq_mhz": round(float(f_mhz), 3),
        "fine": bool(fine),
        "started_at": time.time(),
    }
    try:
        return measure_vf_point_inner(engine, v_mv, f_mhz, fine, wait, n_samples, sample_interval)
    except MinerNotReady as e:
        engine.log(
            f"Phase V cell ({int(v_mv)} mV, {float(f_mhz):.1f} MHz): "
            f"settle failed ({e}) — marking cell thermal_failed so the "
            f"state machine skips it on the next iteration, then "
            f"re-raising for recovery"
        )
        engine.vf_surface.append(
            {
                "voltage_mv": int(v_mv),
                "freq_mhz": round(float(f_mhz), 3),
                "efficiency_jth": 0,
                "hashrate_ths": 0,
                "power_w": 0,
                "fine": bool(fine),
                "thermal_failed": True,
                "settle_failed": True,
                "measured_at": datetime.now().isoformat(),
            }
        )
        try:
            engine._save_checkpoint()
        except Exception as ex:
            engine.log(f"Checkpoint save failed during settle_failed mark (non-fatal): {ex}")
        engine.current_sweep_voltage_mv = None
        raise
    finally:
        engine.current_vf_point = None


def measure_vf_point_inner(engine, v_mv, f_mhz, fine, wait, n_samples, sample_interval):
    engine.current_sweep_voltage_mv = v_mv
    engine.min_voltage_mv = v_mv

    # _phase1_set_voltage handles direction-aware V/F ordering AND issues
    # a uniform set_clock_all(f) as its final step, so alive chips land on
    # `f_mhz` in one round-trip. We only need a follow-up per-chip write
    # when dead chips need to be pinned back to DEAD_CHIP_FREQ.
    engine._phase1_set_voltage(v_mv, f_mhz)
    if not engine.running:
        return None
    engine.phase = engine.PHASE_VF_EXPLORATION
    if any(engine.parked_chips[b] for b in range(engine.num_boards)):
        # Gate the per-chip write on the uniform clock settling. Without
        # this, _apply_uniform_freq's three back-to-back set_clock_chip
        # POSTs can collide with Phase 1's trailing set_clock_all while
        # the firmware is still ramping 324 chips internally, producing
        # "Last command is still pending" errors that used to escalate
        # into a full recovery cycle.
        engine._wait_for_clock_settle([float(f_mhz)] * engine.num_boards)
        engine._apply_uniform_freq(f_mhz)
        engine._wait_for_mining_state(timeout=300)
    else:
        # Keep the in-memory freq arrays in lockstep with what the miner is
        # actually running, so the dashboard heatmap and any subsequent
        # Phase 4 don't show stale values.
        for b in range(engine.num_boards):
            n = (
                len(engine.baseline_scores[b])
                if engine.baseline_scores[b]
                else engine.chips_per_board
            )
            engine.stable_freq_arrays[b] = [float(f_mhz)] * n

    result = {
        "voltage_mv": int(v_mv),
        "freq_mhz": round(float(f_mhz), 3),
        "efficiency_jth": None,
        "hashrate_ths": None,
        "power_w": None,
        "fine": bool(fine),
        "measured_at": datetime.now().isoformat(),
    }

    # Restart loop: if the miner stops hashing mid-sampling, redo the
    # stabilize wait + sample collection from scratch. Partial readings
    # are discarded — they were taken against state that's no longer
    # representative after the hashing break.
    readings = []
    restart_pass = 0
    while engine.running:
        # Defense-in-depth re-apply on restart: firmware may have lost
        # per-chip clocks during the hashing break. Skipped on first
        # pass since Phase 1 already applied V/F above.
        if restart_pass > 0:
            engine.api.set_clock_all(f_mhz)
            engine._wait_for_clock_settle([float(f_mhz)] * engine.num_boards)
            if any(engine.parked_chips[b] for b in range(engine.num_boards)):
                engine._apply_uniform_freq(f_mhz)
                engine._wait_for_mining_state(timeout=300)

        engine.phase_detail = f"Phase V {v_mv} mV / {f_mhz:.1f} MHz: stabilizing ({wait}s)"
        if restart_pass == 0:
            engine.log(f"Phase V: {v_mv} mV, {f_mhz:.1f} MHz — stabilizing {wait}s")
        remaining = wait
        while remaining > 0 and engine.running:
            engine.phase_detail = (
                f"Phase V {v_mv} mV / {f_mhz:.1f} MHz: stabilizing ({remaining}s left)"
            )
            chunk = min(remaining, 10)
            time.sleep(chunk)
            remaining -= chunk
            engine._update_live_data()
            em = engine._detect_thermal_emergency()
            if em:
                engine.current_sweep_voltage_mv = None
                return engine._handle_thermal_in_vf_measure(em, v_mv, f_mhz, fine)
        if not engine.running:
            return None
        engine.phase_detail = f"Phase V {v_mv} mV / {f_mhz:.1f} MHz: sampling (0/{n_samples})"

        # Per-point sanity gate. If the miner is running at a fraction of
        # the expected hashrate — e.g. after a recovery that left a board
        # offline or chips stuck at an unstable carried-forward clock —
        # the samples we'd collect here are garbage and will pollute the
        # J/TH surface + bias the winner. Compare a single pre-sampling
        # reading against 30% of the stock baseline's hashrate (stock
        # captured at S21 stock freq ~490 MHz; even the lowest coarse-grid
        # freq should beat 30% of that). On failure, leave efficiency_jth
        # None and skip the sampling loop entirely — the Phase V orchestrator
        # skips no-data cells in trend logic without counting toward trend
        # confirmation, so one garbage cell won't derail the walk.
        stock_ths = float((engine.stock_baseline or {}).get("hashrate_ths", 0) or 0)
        if stock_ths > 0:
            try:
                pre_summary = engine.api.summary()
            except Exception:
                pre_summary = None
            pre_ths = pre_summary.hashrate_ths if pre_summary else 0
            sanity_min = 0.30 * stock_ths
            if pre_ths < sanity_min:
                engine.log(
                    f"Phase V: ({v_mv} mV, {f_mhz:.1f} MHz) — pre-sample "
                    f"hashrate {pre_ths:.1f} TH/s below sanity threshold "
                    f"{sanity_min:.1f} TH/s (30% of {stock_ths:.1f} stock) — "
                    f"skipping as no-data"
                )
                engine.current_sweep_voltage_mv = None
                return result

        # Collect J/TH samples. Crashed/throttled chips show up as terrible
        # J/TH (low hashrate × same power), so the trend-confirmation skip in
        # the Phase V orchestrator handles "bad" points naturally — no
        # separate stability gate needed.
        readings = []
        restart_needed = False
        for i in range(n_samples):
            if not engine.running:
                break
            engine.phase_detail = (
                f"Phase V {v_mv} mV / {f_mhz:.1f} MHz: sampling ({i + 1}/{n_samples})"
            )
            em = engine._detect_thermal_emergency()
            if em:
                engine.current_sweep_voltage_mv = None
                return engine._handle_thermal_in_vf_measure(em, v_mv, f_mhz, fine)
            if not engine._is_miner_hashing():
                try:
                    engine._wait_for_mining_state(timeout=120)
                except MinerNotReady:
                    engine.log(f"  sample {i + 1}: miner not ready, skipping")
                    continue
                # Mining state recovered — discard partial readings and
                # restart at the top of the cell so the stabilize wait
                # runs again before fresh sampling.
                engine.log(
                    f"Phase V cell ({v_mv} mV, {f_mhz:.1f} MHz): "
                    f"miner stopped hashing during sample collection "
                    f"— restabilizing"
                )
                restart_needed = True
                break
            try:
                summary = engine.api.summary()
            except Exception as ex:
                engine.log(f"  sample {i + 1}: summary fetch failed: {ex}")
                summary = None
            if summary:
                p = summary.power_w
                h = summary.hashrate_ths
                if p > 0 and h > 0:
                    readings.append({"hashrate_ths": h, "power_w": p, "efficiency_jth": p / h})
            if i < n_samples - 1:
                time.sleep(sample_interval)

        if not restart_needed:
            break
        restart_pass += 1

    if not engine.running:
        return None

    if not readings:
        # Fail-closed: no usable samples means we have no efficiency data
        # for this point. Caller (ray walk) skips it for trend tracking
        # but still advances past it. Entry is persisted so resume doesn't
        # retry indefinitely on a point the miner can't reach.
        engine.log(f"Phase V: ({v_mv} mV, {f_mhz:.1f} MHz) — no usable samples, no data")
        engine.current_sweep_voltage_mv = None
        return result

    result["hashrate_ths"] = round(statistics.mean(r["hashrate_ths"] for r in readings), 3)
    result["power_w"] = round(statistics.mean(r["power_w"] for r in readings), 1)
    result["efficiency_jth"] = round(statistics.mean(r["efficiency_jth"] for r in readings), 3)
    engine.log(
        f"Phase V: ({v_mv} mV, {f_mhz:.1f} MHz) -> "
        f"{result['hashrate_ths']:.2f} TH/s, {result['power_w']:.0f} W, "
        f"{result['efficiency_jth']:.2f} J/TH"
    )
    engine.current_sweep_voltage_mv = None
    return result
