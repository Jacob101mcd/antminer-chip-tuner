"""
Free functions extracted from TuningEngine class methods for monitoring and thermal handling.
These functions are designed to be called with an engine instance as the first parameter,
replacing the 'self' reference within the original class methods.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from tuner_app import state
from tuner_app.constants import FIRMWARE_FREQ_MIN_MHZ
from tuner_app.metrics.sampler import build_sample
from tuner_app.miner.exceptions import MinerCommandError, MinerNotReady, MinerOfflineError
from tuner_app.profit.compute import score_cell
from tuner_app.tuning_engine.scoring import get_scoring_context

logger = logging.getLogger(__name__)


def _record_metrics(engine) -> None:
    """Best-effort metrics record after a fresh live-data update.

    The monitor cycle MUST NOT abort on a metrics-write failure — losing one
    sample is preferable to stalling tuning.  Failures are logged at WARNING
    so an operator can still spot a misconfigured / out-of-disk metrics db.
    """
    store = state.metrics_store
    if store is None:
        return
    try:
        store.record_sample(engine.mac, build_sample(engine))
    except Exception as exc:
        logger.warning("metrics record failed for %s: %s", engine.mac, exc)


def do_monitor_cycle(engine):
    """One Phase 6 (perpetual) cycle: PERPETUAL_VOLTAGE_CHECK_MIN-minute
    sleep, then thermal sweep + hashrate drift + voltage adjust. Returns
    when the cycle completes or self.running becomes False.

    First entry into monitor (when not previously announced this session):
    refresh sweep reference, apply active profile, fire MRR maintaining.
    Subsequent cycles within the same monitor session skip that block.

    After this, the outer loop re-evaluates find_next_*. If new work
    appears (settings change exposing more rays/fines/chip-tunes), the
    else-if structure naturally exits monitor on the next iteration.

    Transient-failure containment: a single MinerOfflineError during
    sample/thermal/voltage work is absorbed; only PHASE6_OFFLINE_THRESHOLD
    consecutive offline cycles propagate to _run()'s offline-mode flow.
    Mirrors the legacy _phase6_perpetual's `ph6_offline_hits` logic."""
    try:
        return do_monitor_cycle_body(engine)
    except MinerOfflineError as e:
        engine._monitor_offline_hits = getattr(engine, "_monitor_offline_hits", 0) + 1
        threshold = engine.PHASE6_OFFLINE_THRESHOLD
        engine.log(f"Monitor: transient offline {engine._monitor_offline_hits}/{threshold}: {e}")
        if engine._monitor_offline_hits >= threshold:
            # Sustained outage — escalate to _run()'s offline-mode flow.
            raise
        # Short cooldown before next iteration of the main loop.
        for _ in range(6):
            if not engine.running:
                return
            time.sleep(10)


def re_rank_active_voltage(engine):
    """Phase 6 cross-voltage re-rank. Picks the best voltage_results entry
    under the current scoring context (TARGET_MODE + minerstat snapshot)
    and switches active_sweep_voltage_mv to it via select_voltage_profile
    when the winner differs.

    Called once per Phase 6 monitor cycle so retunes, minerstat refreshes,
    and TARGET_MODE flips propagate without waiting for an operator-
    triggered "Recompute & Apply". No-op when voltage_results is empty,
    when scoring fails for every entry, or when the winner already matches
    the active voltage.
    """
    if not engine.voltage_results:
        return
    try:
        winner = min(engine.voltage_results, key=engine._score_key())
    except (ValueError, TypeError):
        return
    winner_mv = winner.get("voltage_mv") if winner else None
    if winner_mv is None or engine.active_sweep_voltage_mv == winner_mv:
        return
    mode = engine.config.get("TARGET_MODE", "efficiency") or "efficiency"
    engine.log(
        f"Phase 6: re-rank → switching active profile from "
        f"{engine.active_sweep_voltage_mv} mV to {winner_mv} mV "
        f"({mode} scoring)"
    )
    try:
        engine.select_voltage_profile(int(winner_mv))
    except Exception as ex:
        engine.log(f"Phase 6: switch to {winner_mv} mV failed (non-fatal): {ex}")


def do_monitor_cycle_body(engine):
    """The actual monitor cycle work, wrapped by _do_monitor_cycle's
    transient-failure handler. On a clean cycle, resets the consecutive
    offline counter."""
    re_rank_active_voltage(engine)

    if not engine._mrr_phase6_announced:
        engine.phase = engine.PHASE_PERPETUAL
        engine._refresh_sweep_reference()
        if not engine.sweep_voltage_mv:
            # No voltage_results to monitor — wait passively.
            engine.phase_detail = "No tuned profile yet — sleeping"
            remaining = 60
            while remaining > 0 and engine.running:
                time.sleep(min(remaining, 10))
                remaining -= 10
            return
        engine.log(
            f"Monitor: voltage-tracking active (sweep profile "
            f"{engine.sweep_voltage_mv} mV / "
            f"{engine.sweep_hashrate_ths:.2f} TH/s)"
        )
        engine.min_voltage_mv = engine.sweep_voltage_mv + engine.voltage_adjustment_mv
        try:
            engine._apply_stable_freqs()
            engine._wait_for_mining_state(timeout=600)
        except (MinerCommandError, MinerNotReady) as ex:
            engine.log(f"Monitor: initial apply failed: {ex} — retrying next cycle")
        engine._mrr_sync("maintaining", reason="Entered monitor")
        engine._mrr_phase6_announced = True

    engine.phase = engine.PHASE_PERPETUAL

    # Evaluate chip-tune vs fine-tune freq array fallback once per cycle,
    # before the long check-interval sleep. Logs only on transitions.
    _active_mv = engine.active_sweep_voltage_mv
    active_entry = next(
        (r for r in engine.voltage_results if r.get("voltage_mv") == _active_mv),
        None,
    )
    # Bixbit: chip-tune fallback evaluation is skipped — chip_tune_active is
    # always False on Bixbit (Bixbit auto-tunes per-chip internally). The
    # evaluate_chip_tune_fallback function also guards against chip_tune_active=
    # False entries internally, but the explicit skip here makes the vendor
    # split clear and avoids an unnecessary function call.
    if engine.api.supports_per_chip_tuning():
        evaluate_chip_tune_fallback(engine, active_entry)

    check_min = engine.config["PERPETUAL_VOLTAGE_CHECK_MIN"]
    check_interval_sec = check_min * 60
    deadband_pct = engine.config["PERPETUAL_HASHRATE_DEADBAND_PCT"]
    step_mv = engine.config["PERPETUAL_VOLTAGE_STEP_MV"]

    # Sleep first so the cycle has fresh hashrate samples to work with.
    engine.phase_detail = f"Monitoring (next check in {check_min} min)"
    remaining = check_interval_sec
    while remaining > 0 and engine.running:
        time.sleep(min(remaining, 10))
        remaining -= 10
        engine._update_live_data()
        # Persist a metrics sample for this miner.  Best-effort — failure
        # never aborts the cycle.  The (mac, ts) PK + INSERT OR REPLACE
        # makes duplicate-ts harmless if _update_live_data's internal
        # 5-second rate gate skips the actual API fetch on this iteration.
        _record_metrics(engine)
        # Inline thermal sweep — chips are at steady state in monitor
        # mode, no pending clock ramp, so the existing throttle path
        # (set_clock_chip without stop_mining) is safe. Rate-limited
        # via the same `_last_thermal_check` field as in-tune detect
        # so we don't fire /temps + /temps/chip every 10s. Brings
        # emergency response down from up-to-10-min to ~30s.
        if detect_thermal_emergency(engine) is not None:
            try:
                perpetual_thermal_sweep(engine)
            except Exception as ex:
                engine.log(f"Monitor inline thermal sweep failed: {ex}")
    if not engine.running:
        return

    if not engine._is_miner_hashing():
        engine.log("Monitor: miner not hashing, waiting for recovery before check")
        try:
            engine._wait_for_mining_state(timeout=600)
        except MinerNotReady as ex:
            engine.log(f"Monitor: miner did not return to Mining ({ex}) — skipping cycle")
            return

    engine.phase_detail = "Sampling hashrate"
    avg_ths = engine._perpetual_sample_hashrate(check_min)

    engine.phase_detail = "Thermal safety sweep"
    thermal_changed = perpetual_thermal_sweep(engine)

    voltage_changed = False
    if engine.sweep_hashrate_ths > 0 and avg_ths > 0:
        delta_pct = (avg_ths - engine.sweep_hashrate_ths) / engine.sweep_hashrate_ths * 100
        engine.log(
            f"Monitor: avg {avg_ths:.2f} TH/s vs sweep "
            f"{engine.sweep_hashrate_ths:.2f} TH/s "
            f"(delta {delta_pct:+.2f}%, deadband ±{deadband_pct}%, "
            f"adjustment {engine.voltage_adjustment_mv:+d} mV)"
        )
        if abs(delta_pct) <= deadband_pct:
            engine.log("Monitor: within deadband — no voltage change")
        elif delta_pct < -deadband_pct:
            engine.log(f"Monitor: hashrate below target, nudging voltage up {step_mv} mV")
            voltage_changed = engine._adjust_voltage(+step_mv)
        else:
            engine.log(f"Monitor: hashrate above target, nudging voltage down {step_mv} mV")
            voltage_changed = engine._adjust_voltage(-step_mv)
    else:
        engine.log(
            f"Monitor: hashrate sample unavailable "
            f"(avg={avg_ths:.2f}, sweep={engine.sweep_hashrate_ths:.2f}) "
            f"— no change"
        )

    if thermal_changed or voltage_changed:
        try:
            engine._save_profile()
        except Exception as ex:
            engine.log(f"Monitor: profile save failed (non-fatal): {ex}")

    try:
        summary = engine.api.summary()
    except Exception:
        summary = None
    if summary:
        power_w = summary.power_w
        hashrate_ths = summary.hashrate_ths
        if hashrate_ths > 0 and power_w > 0:
            efficiency = power_w / hashrate_ths
            if engine.best_efficiency is None or efficiency < engine.best_efficiency:
                engine.best_efficiency = efficiency

    # Clean cycle — reset the consecutive-offline counter. Used by
    # _do_monitor_cycle's MinerOfflineError handler to escalate after
    # PHASE6_OFFLINE_THRESHOLD consecutive blips.
    engine._monitor_offline_hits = 0


def evaluate_chip_tune_fallback(engine, active_entry):
    """Evaluate whether chip-tuned or fine-tune (uniform) freq arrays score
    better under the current TARGET_MODE + minerstat snapshot.

    Called once per Phase 6 (PHASE_PERPETUAL) monitor cycle after the
    initial MRR announce block, before the long check-interval sleep.

    Logs ONE line per TRANSITION only (not per cycle).
    Does NOT revert chip_max -- that anti-oscillation knowledge is preserved.
    """
    # Step 1: early return if no fine_tune_freq_arrays
    if active_entry is None or active_entry.get("fine_tune_freq_arrays") is None:
        return
    # Step 2: get scoring context
    ctx = get_scoring_context(engine)
    # Step 3: build chip_entry (uses current chip-tuned metrics from active_entry)
    chip_entry = {
        "voltage_mv": active_entry.get("voltage_mv"),
        "efficiency_jth": active_entry.get("efficiency_jth"),
        "hashrate_ths": active_entry.get("hashrate_ths"),
        "power_w": active_entry.get("power_w"),
        "thermal_failed": False,
    }
    # Step 4: build fine_entry (uses fine_tune_* siblings)
    fine_entry = {
        "voltage_mv": active_entry.get("voltage_mv"),
        "efficiency_jth": active_entry.get("fine_tune_efficiency_jth"),
        "hashrate_ths": active_entry.get("fine_tune_hashrate_ths"),
        "power_w": active_entry.get("fine_tune_power_w"),
        "thermal_failed": False,
    }
    # Step 5: score both
    score_chip = score_cell(chip_entry, *ctx)
    score_fine = score_cell(fine_entry, *ctx)
    # Step 6: if either score is None, return
    if score_chip is None or score_fine is None:
        return
    # Step 7: determine better variant (lower score = better)
    better = "chip" if score_chip <= score_fine else "fine"
    # Step 8: current state
    currently_chip = active_entry.get("chip_tune_active", True)
    # Step 9: no change if better matches current -- no log, no flip
    if (better == "chip") == currently_chip:
        return
    # Step 10: FLIP
    active_entry["chip_tune_active"] = better == "chip"
    active_entry["last_fallback_decision_ts"] = time.time()
    active_entry["last_fallback_score_delta"] = score_chip - score_fine
    if better == "chip":
        engine.stable_freq_arrays = [arr[:] for arr in active_entry["stable_freq_arrays"]]
    else:
        engine.stable_freq_arrays = [arr[:] for arr in active_entry["fine_tune_freq_arrays"]]
    engine._apply_stable_freqs()
    engine._save_profile()
    engine.log(
        f"Phase 6 fallback: switched to "
        f"{'chip-tune' if better == 'chip' else 'fine-tune'} arrays "
        f"(score delta {active_entry['last_fallback_score_delta']:+.4f})"
    )


def perpetual_thermal_sweep(engine):
    """Board/chip thermal safety check. Throttles hot chips via per-chip
    clock API (no restart). Returns True if any freq changed."""
    board_temps = engine._get_board_temps()
    chip_temps = engine._get_chip_temps()
    board_max = engine.config["BOARD_MAX_TEMP"]
    chip_crit = engine.config["CHIP_CRITICAL_TEMP"]
    emergency_step = engine.config["FREQ_STEP_EMERGENCY"]
    # Thermal throttle's hard floor is the firmware minimum (50 MHz), not a
    # tuning knob. Chips that need more than this are hardware-failing and
    # will be rescued by DEAD_CHIP_FREQ parking on the next Phase 2.
    freq_floor = FIRMWARE_FREQ_MIN_MHZ
    changed = False

    for b in range(engine.num_boards):
        if not engine.stable_freq_arrays[b]:
            continue

        if b < len(board_temps) and board_temps[b] > board_max:
            engine.log(
                f"EMERGENCY: Board {b} temp {board_temps[b]:.1f}C > {board_max}C — "
                f"throttling all chips by {emergency_step} MHz (no restart)"
            )
            chip_writes = []
            for chip_idx in range(len(engine.stable_freq_arrays[b])):
                new_freq = max(engine.stable_freq_arrays[b][chip_idx] - emergency_step, freq_floor)
                if new_freq != engine.stable_freq_arrays[b][chip_idx]:
                    engine.stable_freq_arrays[b][chip_idx] = new_freq
                    changed = True
                chip_writes.append((chip_idx, new_freq))
            try:
                engine.api.set_clock_chip(b, chip_writes)
            except MinerCommandError as ex:
                engine.log(f"Perpetual: board {b} throttle write failed: {ex}")
            continue

        if not chip_temps or b >= len(chip_temps):
            continue
        ct = chip_temps[b]
        hot_writes = []
        for chip_idx in range(min(len(engine.stable_freq_arrays[b]), len(ct))):
            temp = ct[chip_idx]
            if not isinstance(temp, (int, float)) or temp <= chip_crit:
                continue
            new_freq = max(engine.stable_freq_arrays[b][chip_idx] - emergency_step, freq_floor)
            if new_freq != engine.stable_freq_arrays[b][chip_idx]:
                engine.stable_freq_arrays[b][chip_idx] = new_freq
                hot_writes.append((chip_idx, new_freq, temp))
                changed = True
        if hot_writes:
            summary = ", ".join(f"chip {i}@{t:.0f}C->{f}MHz" for i, f, t in hot_writes[:5])
            more = "..." if len(hot_writes) > 5 else ""
            engine.log(
                f"Perpetual: board {b} throttling {len(hot_writes)} chip(s) "
                f"above {chip_crit}C: {summary}{more}"
            )
            try:
                engine.api.set_clock_chip(b, [(i, f) for i, f, _ in hot_writes])
            except MinerCommandError as ex:
                engine.log(f"Perpetual: board {b} chip throttle write failed: {ex}")
    return changed


def detect_thermal_emergency(engine, min_interval_sec=30):
    """Rate-limited emergency-temp detector. Returns
    {boards: [b, ...], chips: [(b, chip_idx, temp), ...]} when something
    has crossed BOARD_MAX_TEMP / CHIP_CRITICAL_TEMP, or None if
    everything's fine OR the rate limit hasn't elapsed since last call.

    Pure read — does NOT mutate stable_freq_arrays or call set_clock_chip.
    Wired into the long wait/sample loops in Phase 2/V/3/3b/4 + monitor
    mode; the actual response is dispatched by phase-specific handlers
    (`_handle_thermal_in_chip_tune`, `_handle_thermal_in_vf_measure`)
    because the right action varies per phase. Swallows MinerOfflineError
    so a transient API hiccup doesn't abort the in-flight wait loop."""
    now = time.time()
    last = getattr(engine, "_last_thermal_check", 0)
    if now - last < min_interval_sec:
        return None
    engine._last_thermal_check = now
    try:
        board_temps = engine._get_board_temps()
        chip_temps = engine._get_chip_temps()
    except MinerOfflineError as ex:
        engine.log(f"Thermal detect: API offline, skipping ({ex})")
        return None
    except Exception as ex:
        engine.log(f"Thermal detect: read failed, skipping ({ex})")
        return None
    board_max = engine.config["BOARD_MAX_TEMP"]
    chip_crit = engine.config["CHIP_CRITICAL_TEMP"]
    hot_boards = []
    hot_chips = []
    for b in range(engine.num_boards):
        if b < len(board_temps) and board_temps[b] > board_max:
            hot_boards.append(b)
            continue
        if not chip_temps or b >= len(chip_temps):
            continue
        for chip_idx, temp in enumerate(chip_temps[b]):
            if isinstance(temp, (int, float)) and temp > chip_crit:
                hot_chips.append((b, chip_idx, temp))
    if hot_boards or hot_chips:
        return {"boards": hot_boards, "chips": hot_chips}
    return None


def handle_thermal_in_chip_tune(engine, emergency):
    """Phase 2 / Phase 3 / Phase 3b thermal handler. Drops offending
    chip(s) by FREQ_STEP_EMERGENCY (board emergency = drop every chip on
    that board); when chip_max is in scope (Phase 3 active), pins
    chip_max[b][i] to the chip's PRE-throttle freq so the iterative
    loop's UP-step never retries the hot freq. Stops mining first to
    drain the firmware command queue, then restarts.

    Returns True if any freq was actually changed."""
    step = float(engine.config["FREQ_STEP_EMERGENCY"])
    floor = float(FIRMWARE_FREQ_MIN_MHZ)
    n_boards_hot = len(emergency.get("boards", []))
    n_chips_hot = len(emergency.get("chips", []))
    engine.log(
        f"EMERGENCY (in-tune): {n_boards_hot} board(s) + "
        f"{n_chips_hot} chip(s) over limit — stop_mining + "
        f"throttle + start_mining"
    )
    try:
        engine.api.stop_mining()
    except MinerCommandError as ex:
        engine.log(f"Thermal handler: stop_mining failed: {ex} — continuing anyway")
    engine._drain_firmware_command_queue()

    has_chip_max = bool(getattr(engine, "chip_max", None))
    changed = False
    for b in emergency.get("boards", []):
        if b >= len(engine.stable_freq_arrays):
            continue
        for chip_idx in range(len(engine.stable_freq_arrays[b])):
            cur = engine.stable_freq_arrays[b][chip_idx]
            new_freq = max(cur - step, floor)
            if new_freq != cur:
                engine.stable_freq_arrays[b][chip_idx] = new_freq
                changed = True
            if has_chip_max and b < len(engine.chip_max) and chip_idx < len(engine.chip_max[b]):  # noqa: SIM102
                if engine.chip_max[b][chip_idx] is None or cur < engine.chip_max[b][chip_idx]:
                    engine.chip_max[b][chip_idx] = cur
    for b, chip_idx, _temp in emergency.get("chips", []):
        if b >= len(engine.stable_freq_arrays) or chip_idx >= len(engine.stable_freq_arrays[b]):
            continue
        cur = engine.stable_freq_arrays[b][chip_idx]
        new_freq = max(cur - step, floor)
        if new_freq != cur:
            engine.stable_freq_arrays[b][chip_idx] = new_freq
            changed = True
        if has_chip_max and b < len(engine.chip_max) and chip_idx < len(engine.chip_max[b]):  # noqa: SIM102
            if engine.chip_max[b][chip_idx] is None or cur < engine.chip_max[b][chip_idx]:
                engine.chip_max[b][chip_idx] = cur

    try:
        engine.api.start_mining()
    except MinerCommandError as ex:
        engine.log(f"Thermal handler: start_mining failed: {ex} — caller will retry")
    try:
        engine._wait_for_mining_state(timeout=600)
    except MinerNotReady as ex:
        engine.log(f"Thermal handler: miner did not return to Mining ({ex})")
        return changed
    # Apply the throttled freqs now that mining is back up.
    try:
        engine._apply_stable_freqs()
    except MinerCommandError as ex:
        engine.log(f"Thermal handler: apply_stable_freqs failed: {ex}")
    try:
        engine._save_checkpoint()
    except Exception as ex:
        engine.log(f"Thermal handler: checkpoint save failed: {ex}")
    return changed


def handle_thermal_in_vf_measure(engine, emergency, v_mv, f_mhz, fine):
    """Phase V / Phase 4 thermal handler. Synthesizes a thermal_failed
    result for the cell so ranking treats it as worst (effectively
    skipped) while the measurement record persists for dashboard
    rendering and remeasure-queue retries. Stops + restarts mining so
    the next cell starts clean."""
    n_boards_hot = len(emergency.get("boards", []))
    n_chips_hot = len(emergency.get("chips", []))
    engine.log(
        f"EMERGENCY (Phase V/4): cell ({v_mv} mV, {f_mhz:.1f} MHz) "
        f"overheated — {n_boards_hot} board(s) + {n_chips_hot} chip(s) "
        f"— stop_mining, mark cell thermal_failed"
    )
    try:
        engine.api.stop_mining()
    except MinerCommandError as ex:
        engine.log(f"Thermal handler: stop_mining failed: {ex} — continuing anyway")
    engine._drain_firmware_command_queue()
    try:
        engine.api.start_mining()
    except MinerCommandError as ex:
        engine.log(f"Thermal handler: start_mining failed: {ex} — caller will retry")
    try:
        engine._wait_for_mining_state(timeout=600)
    except MinerNotReady as ex:
        engine.log(f"Thermal handler: miner did not return to Mining ({ex})")
    return {
        "voltage_mv": int(v_mv),
        "freq_mhz": round(float(f_mhz), 3),
        "efficiency_jth": 0,
        "hashrate_ths": 0,
        "power_w": 0,
        "fine": bool(fine),
        "thermal_failed": True,
        "measured_at": datetime.now().isoformat(),
    }
