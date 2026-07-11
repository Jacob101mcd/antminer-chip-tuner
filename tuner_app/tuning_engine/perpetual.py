"""
Perpetual voltage tracking and adjustment logic for mining rigs.
Handles continuous monitoring, voltage adjustment, and restart procedures
based on hashrate performance relative to sweep profile targets.
"""

from __future__ import annotations

import statistics
import time

from tuner_app.miner.exceptions import (
    MinerCommandError,
    MinerNotReady,
    MinerOfflineError,
    UnsafeVoltageBoundsError,
)
from tuner_app.tuning_engine.voltage_safety import require_voltage_mutation_allowed


def phase6_perpetual(engine):
    engine.phase = engine.PHASE_PERPETUAL
    engine._refresh_sweep_reference()
    if not engine.sweep_voltage_mv:
        engine.log("Phase 6: no sweep profile available — exiting")
        return
    engine.log(
        f"Phase 6: voltage-tracking perpetual tune (sweep profile "
        f"{engine.sweep_voltage_mv} mV / {engine.sweep_hashrate_ths:.2f} TH/s)"
    )

    # Apply the active sweep profile's target voltage on entry so the adjuster
    # starts from a known state. If voltage_adjustment_mv was persisted from a
    # prior session, honor it so we don't discard valid drift tracking.
    engine.min_voltage_mv = engine.sweep_voltage_mv + engine.voltage_adjustment_mv
    try:
        engine._apply_stable_freqs()
        engine._wait_for_mining_state(timeout=600)
    except UnsafeVoltageBoundsError:
        raise
    except (MinerCommandError, MinerNotReady) as ex:
        engine.log(f"Phase 6: initial apply failed: {ex} — retrying on next cycle")

    # MRR: flip rig to enabled + push advertised hashrate. Once per
    # Phase 6 session — the flag is cleared on exit (stop/error). No-op
    # if MRR is disabled or rig isn't configured.
    if not engine._mrr_phase6_announced:
        engine._mrr_sync("maintaining", reason="Entered Phase 6")
        engine._mrr_phase6_announced = True

    check_min = engine.config["PERPETUAL_VOLTAGE_CHECK_MIN"]
    check_interval_sec = check_min * 60
    deadband_pct = engine.config["PERPETUAL_HASHRATE_DEADBAND_PCT"]
    step_mv = engine.config["PERPETUAL_VOLTAGE_STEP_MV"]

    # Transient-failure containment. A single network blip during
    # _update_live_data() or api.summary() used to unwind all the way to
    # _run(), which then re-entered _run_inner() from the top and re-logged
    # "Found saved tuning profile, entering perpetual tune". With a 10-min
    # cycle and 1-min transient blips that hit 20+ times in a session, the
    # cascade made the log unreadable. Now we swallow transients here and
    # only escalate to _run()'s offline-mode machinery after
    # PHASE6_OFFLINE_THRESHOLD consecutive cycles fail (~30 min default).
    ph6_offline_hits = 0
    ph6_offline_threshold = engine.PHASE6_OFFLINE_THRESHOLD

    while engine.running:
        try:
            engine.phase_detail = f"Monitoring (next check in {check_min} min)"
            remaining = check_interval_sec
            while remaining > 0 and engine.running:
                time.sleep(min(remaining, 10))
                remaining -= 10
                engine._update_live_data()
            if not engine.running:
                return

            if not engine._is_miner_hashing():
                engine.log("Perpetual: miner not hashing, waiting for recovery before check")
                try:
                    engine._wait_for_mining_state(timeout=600)
                except MinerNotReady as ex:
                    engine.log(f"Perpetual: miner did not return to Mining ({ex}) — skipping cycle")
                    continue

            engine.phase_detail = "Sampling hashrate"
            avg_ths = engine._perpetual_sample_hashrate(check_min)

            engine.phase_detail = "Thermal safety sweep"
            thermal_changed = engine._perpetual_thermal_sweep()

            voltage_changed = False
            if engine.sweep_hashrate_ths > 0 and avg_ths > 0:
                delta_pct = (avg_ths - engine.sweep_hashrate_ths) / engine.sweep_hashrate_ths * 100
                engine.log(
                    f"Perpetual: avg {avg_ths:.2f} TH/s vs sweep {engine.sweep_hashrate_ths:.2f} "
                    f"TH/s (delta {delta_pct:+.2f}%, deadband ±{deadband_pct}%, "
                    f"adjustment {engine.voltage_adjustment_mv:+d} mV)"
                )
                if abs(delta_pct) <= deadband_pct:
                    engine.log("Perpetual: within deadband — no voltage change")
                elif delta_pct < -deadband_pct:
                    engine.log(f"Perpetual: hashrate below target, nudging voltage up {step_mv} mV")
                    voltage_changed = engine._adjust_voltage(+step_mv)
                else:
                    engine.log(
                        f"Perpetual: hashrate above target, nudging voltage down {step_mv} mV"
                    )
                    voltage_changed = engine._adjust_voltage(-step_mv)
            else:
                engine.log(
                    f"Perpetual: hashrate sample unavailable "
                    f"(avg={avg_ths:.2f}, sweep={engine.sweep_hashrate_ths:.2f}) — no change"
                )

            if thermal_changed or voltage_changed:
                try:
                    engine._save_profile()
                except Exception as ex:
                    engine.log(f"Perpetual: profile save failed (non-fatal): {ex}")

            summary = engine.api.summary()
            if summary:
                power_w = summary.power_w
                hashrate_ths = summary.hashrate_ths
                if hashrate_ths > 0 and power_w > 0:
                    efficiency = power_w / hashrate_ths
                    if engine.best_efficiency is None or efficiency < engine.best_efficiency:
                        engine.best_efficiency = efficiency

            # Clean cycle — reset the transient counter.
            ph6_offline_hits = 0

        except MinerOfflineError as e:
            ph6_offline_hits += 1
            engine.log(f"Phase 6 transient offline {ph6_offline_hits}/{ph6_offline_threshold}: {e}")
            if ph6_offline_hits >= ph6_offline_threshold:
                # Real outage — escalate to _run()'s offline-mode flow.
                raise
            # Short cooldown, stay in loop.
            for _ in range(6):
                if not engine.running:
                    return
                time.sleep(10)


def perpetual_sample_hashrate(engine, window_min):
    """Average realtime hashrate (TH/s) over the last window_min minutes.
    Uses /hashrate/history/continuous (10-second interval samples), falling
    back to a single /summary read if history is unavailable."""
    history = None
    try:
        history = engine.api.hashrate_history()
    except Exception as ex:
        engine.log(f"Perpetual: hashrate history fetch failed: {ex}")
    samples_needed = max(1, int(window_min * 60 / 10))
    if history:
        recent = history[-samples_needed:] if len(history) > samples_needed else history
        values = []
        for s in recent:
            if isinstance(s, (int, float)):
                values.append(s)
            elif isinstance(s, dict):
                for key in ("hashrate", "value", "ths", "Hashrate"):
                    if key in s and isinstance(s[key], (int, float)):
                        values.append(s[key])
                        break
        if values:
            avg = statistics.mean(values)
            # History samples may be MH/s or TH/s — detect by magnitude.
            # 10^5 TH/s is fantasy; 10^5 MH/s ≈ 0.1 TH/s which is sane.
            if avg > 1000:
                return avg / 1e6
            return avg
    summary = engine.api.summary()
    if summary:
        return summary.hashrate_ths
    return 0.0


def adjust_voltage(engine, direction_mv):
    """Move the voltage adjuster by direction_mv (signed). Hitting the positive
    cap triggers a gated restart (respecting PERPETUAL_RESTART_MIN_HOURS).
    Returns True if voltage was changed or a restart happened."""
    proposed = engine.voltage_adjustment_mv + direction_mv
    max_delta = engine.config["PERPETUAL_VOLTAGE_MAX_DELTA_MV"]
    min_hrs = engine.config["PERPETUAL_RESTART_MIN_HOURS"]

    if proposed > max_delta:
        now = time.time()
        if engine.last_restart_ts is None:
            elapsed_hrs = float("inf")
        else:
            elapsed_hrs = (now - engine.last_restart_ts) / 3600
        if elapsed_hrs >= min_hrs:
            engine.log(
                f"Perpetual: voltage adjuster saturated at +{max_delta} mV, "
                f"rate-limit elapsed ({elapsed_hrs:.1f} hrs >= {min_hrs} hrs) "
                f"— restarting miner to reset profile"
            )
            return engine._do_perpetual_restart()
        engine.log(
            f"Perpetual: at positive voltage cap (+{max_delta} mV) — "
            f"rate-limited, {min_hrs - elapsed_hrs:.1f} hrs until restart allowed"
        )
        return False

    if proposed < -max_delta:
        if engine.voltage_adjustment_mv <= -max_delta:
            engine.log(f"Perpetual: at negative voltage cap (-{max_delta} mV), clamped")
            return False
        proposed = -max_delta
        engine.log(f"Perpetual: clamped adjustment to negative cap (-{max_delta} mV)")

    target_mv = engine.sweep_voltage_mv + proposed
    psu_min = engine.start_voltage_mv if engine.start_voltage_mv > 0 else 11877
    clamped = max(psu_min, min(engine.psu_max_mv, target_mv))
    if clamped != target_mv:
        engine.log(
            f"Perpetual: target {target_mv} mV clamped to PSU range "
            f"[{psu_min}, {engine.psu_max_mv}] -> {clamped} mV"
        )
        target_mv = clamped
        proposed = target_mv - engine.sweep_voltage_mv

    try:
        require_voltage_mutation_allowed(engine, target_mv)
        engine.api.set_voltage(target_mv)
        engine.min_voltage_mv = target_mv
        engine._wait_for_voltage_settle(target_mv)
        engine.voltage_adjustment_mv = proposed
        engine.log(
            f"Perpetual: voltage set to {target_mv} mV "
            f"(sweep {engine.sweep_voltage_mv} mV {proposed:+d} mV)"
        )
        return True
    except UnsafeVoltageBoundsError:
        raise
    except MinerCommandError as ex:
        engine.log(f"Perpetual: voltage adjustment failed: {ex}")
        return False


def do_perpetual_restart(engine):
    """Revert voltage + chip freqs to the active sweep profile and cycle the
    miner. Resets voltage_adjustment_mv to 0 and stamps last_restart_ts."""
    engine.phase_detail = "Perpetual restart (reverting to sweep profile)"
    engine.log("Perpetual restart: reverting to active sweep profile")
    engine.min_voltage_mv = engine.sweep_voltage_mv
    if engine.sweep_freq_arrays and any(engine.sweep_freq_arrays):
        engine.stable_freq_arrays = [list(arr) for arr in engine.sweep_freq_arrays]

    try:
        engine._apply_stable_freqs()
        engine._wait_for_mining_state(timeout=600)
        engine.api.stop_mining()
        time.sleep(engine.config["RESET_STOP_WAIT"])
        engine.api.start_mining()
        time.sleep(engine.config["RESET_START_WAIT"])
        engine._wait_for_mining_state(timeout=600)
        engine.voltage_adjustment_mv = 0
        engine.last_restart_ts = time.time()
        engine._save_profile()
        engine.log(
            f"Perpetual restart complete (voltage {engine.sweep_voltage_mv} mV, "
            f"adjustment reset to 0)"
        )
        return True
    except UnsafeVoltageBoundsError:
        raise
    except (MinerCommandError, MinerNotReady) as ex:
        engine.log(
            f"Perpetual restart failed: {ex} — adjustment state preserved, will retry on next cycle"
        )
        return False
