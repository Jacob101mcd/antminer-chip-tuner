"""
Persistence: stock baseline + profile + checkpoint disk I/O for TuningEngine.

Path resolution (v4):
  - Profile / checkpoint / stock baseline are per-platform — saved separately
    per (mac, firmware) so reflashing a miner from e.g. LuxOS to Braiins
    doesn't lose the prior firmware's tuning state.
  - Log files are MAC-only (cross-platform) — survive reflash so the operator
    sees one continuous timeline regardless of firmware.

Legacy fallback: when ``engine.mac`` doesn't validate as a canonical MAC or
synth ID (e.g., a test fixture passing an IP, or a v3 in-memory entry that
predates A5 migration), the per-platform helpers fall back to the legacy
``_miner_data_path(engine.mac, suffix)`` shape so existing tests / pre-A9
manager call sites keep working through the PR3 transition.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from tuner_app.config.persistence import _atomic_json_write
from tuner_app.constants import _miner_data_path, _miner_platform_path
from tuner_app.privacy import sanitize


def profile_path(engine):
    """Per-platform profile path: tuning_data/{mac-dashes}.{fw}.profile.json.

    Falls back to legacy ``{ip-dashes}.json`` when engine.mac fails MAC
    validation (test fixtures, pre-migration entries).
    """
    try:
        return _miner_platform_path(engine.mac, engine.firmware_type, ".profile.json")
    except (TypeError, ValueError):
        return _miner_data_path(engine.mac, ".json")


def checkpoint_path(engine):
    """Per-platform checkpoint path with the same legacy-fallback shape as profile."""
    try:
        return _miner_platform_path(engine.mac, engine.firmware_type, ".checkpoint.json")
    except (TypeError, ValueError):
        return _miner_data_path(engine.mac, ".checkpoint.json")


def stock_file(engine):
    """Per-platform stock baseline path with the same legacy-fallback shape."""
    try:
        return _miner_platform_path(engine.mac, engine.firmware_type, ".stock.json")
    except (TypeError, ValueError):
        return _miner_data_path(engine.mac, ".stock.json")


def save_stock_baseline(engine):
    """Persist stock_baseline to its own file (separate from profile/
    checkpoint) so it survives /tuner/delete_profile. Only a successful
    live capture or an explicit spec-fallback should call this."""
    try:
        _atomic_json_write(stock_file(engine), engine.stock_baseline)
    except Exception as e:
        engine.log(f"Warning: failed to save stock baseline: {e}")


def load_stock_baseline(engine):
    """Load the standalone stock baseline file if present. Takes precedence
    over the in-memory __init__ default (no source key) so a fresh engine
    created by /tuner/delete_profile restores the original capture rather
    than re-sampling against a now-tuned miner."""
    path = stock_file(engine)
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        engine.log(f"Warning: failed to read stock baseline: {e}")
        return
    if isinstance(data, dict) and data.get("source") in ("live", "spec", "manual"):
        engine.stock_baseline = data


def restore_saved_state(engine):
    """Load profile or checkpoint from disk so dashboard shows progress on restart."""
    # Stock baseline loads regardless of profile/checkpoint — it's the one
    # piece of state that should outlive a Reset Profile.
    load_stock_baseline(engine)
    # Persisted log file — also survives restarts, wiped only on reset/remove.
    engine._load_log_from_disk()
    saved = load_profile(engine)
    if saved:
        engine.min_voltage_mv = saved.get("min_voltage_mv", 0)
        # Profile may predate num_boards/chips_per_board persistence — adopt
        # the saved values if present so _empty_board_arrays below allocates
        # to the right shape before _resize_board_arrays runs in Phase 0.
        engine.num_boards = saved.get("num_boards", engine.num_boards)
        engine.chips_per_board = saved.get("chips_per_board", engine.chips_per_board)
        engine.baseline_scores = saved.get("baseline_scores") or engine._empty_board_arrays()
        engine.baseline_chip_temps = (
            saved.get("baseline_chip_temps") or engine._empty_board_arrays()
        )
        engine.baseline_chip_hashrates = (
            saved.get("baseline_chip_hashrates") or engine._empty_board_arrays()
        )
        engine.baseline_freq_arrays = (
            saved.get("baseline_freq_arrays") or engine._empty_board_arrays()
        )
        engine.stable_freq_arrays = saved.get("stable_freq_arrays") or engine._empty_board_arrays()
        engine.stock_baseline = saved.get("stock_baseline", engine.stock_baseline)
        engine.best_efficiency = saved.get("best_efficiency")
        engine.voltage_results = saved.get("voltage_results", [])
        engine.wattage_results = saved.get("wattage_results", [])
        engine.wattage_search_low = saved.get("wattage_search_low")
        engine.wattage_search_high = saved.get("wattage_search_high")
        engine.best_wattage_w = saved.get("best_wattage_w")
        # Whatsminer (stock MicroBT) firmware state — additive load with
        # safe defaults for legacy profiles that predate the 2D grid-search
        # algorithm. These fields are only populated for whatsminer miners.
        engine.whatsminer_baselines = saved.get("whatsminer_baselines")
        engine.whatsminer_freq_pct_anchor = saved.get("whatsminer_freq_pct_anchor")
        engine.whatsminer_results = saved.get("whatsminer_results", [])
        engine.whatsminer_pre_tune = saved.get("whatsminer_pre_tune")
        engine.whatsminer_best_cell = saved.get("whatsminer_best_cell")
        engine._wm_current_mode = saved.get("_wm_current_mode")
        engine._wm_current_percent = saved.get("_wm_current_percent")
        engine._wm_current_power_limit = saved.get("_wm_current_power_limit")
        engine._wm_drift_streak = saved.get("_wm_drift_streak", 0)
        engine.vf_surface = saved.get("vf_surface", [])
        engine.vf_planned_grid = saved.get("vf_planned_grid", [])
        engine.in_flight_chip_tune_target = saved.get("in_flight_chip_tune_target")
        # Legacy fields silently ignored on load — left here as `.get()`
        # defaults so old profiles still parse, but no longer used by the
        # dynamic state machine.
        engine.vf_top_k_voltages = saved.get("vf_top_k_voltages", [])
        engine.vf_skipped = saved.get("vf_skipped", [])
        engine.vf_fine_anchor = saved.get("vf_fine_anchor")
        engine.vf_coarse_rays_checked = saved.get("vf_coarse_rays_checked", [])
        engine.remeasure_queue = saved.get("remeasure_queue", [])
        engine.config_snapshot = sanitize(saved.get("config_snapshot"))
        engine.active_sweep_voltage_mv = saved.get("active_sweep_voltage_mv")
        engine.voltage_adjustment_mv = saved.get("voltage_adjustment_mv", 0)
        engine.last_restart_ts = saved.get("last_restart_ts")
        engine.mrr_last_sync = saved.get("mrr_last_sync")
        engine._refresh_sweep_reference()
        engine.tuning_complete = True
        return
    checkpoint = load_checkpoint(engine)
    if checkpoint:
        engine.min_voltage_mv = checkpoint.get("min_voltage_mv", 0)
        engine.start_voltage_mv = checkpoint.get("start_voltage_mv", 0)
        engine.psu_max_mv = checkpoint.get("psu_max_mv", 15182)
        # Adopt saved topology early so empty-array fallbacks below allocate
        # the correct outer length. Phase 0 overwrites from /capabilities
        # and _resize_board_arrays reshapes if the miner changed.
        engine.num_boards = checkpoint.get("num_boards", engine.num_boards)
        engine.chips_per_board = checkpoint.get("chips_per_board", engine.chips_per_board)
        engine.baseline_scores = checkpoint.get("baseline_scores") or engine._empty_board_arrays()
        engine.baseline_chip_temps = (
            checkpoint.get("baseline_chip_temps") or engine._empty_board_arrays()
        )
        engine.baseline_chip_hashrates = (
            checkpoint.get("baseline_chip_hashrates") or engine._empty_board_arrays()
        )
        engine.baseline_freq_arrays = (
            checkpoint.get("baseline_freq_arrays") or engine._empty_board_arrays()
        )
        engine.stable_freq_arrays = (
            checkpoint.get("stable_freq_arrays") or engine._empty_board_arrays()
        )
        # Authoritative dynamic-state-machine state
        engine.vf_surface = checkpoint.get("vf_surface", [])
        engine.vf_planned_grid = checkpoint.get("vf_planned_grid", [])
        engine.in_flight_chip_tune_target = checkpoint.get("in_flight_chip_tune_target")
        engine.remeasure_queue = checkpoint.get("remeasure_queue", [])
        # Legacy fields silently ignored on load — left here as `.get()`
        # defaults so old checkpoints still parse, but the new state
        # machine doesn't read them for any decision.
        engine.vf_top_k_voltages = checkpoint.get("vf_top_k_voltages", [])
        engine.vf_refinement_index = checkpoint.get("vf_refinement_index")
        engine.vf_skipped = checkpoint.get("vf_skipped", [])
        engine.vf_fine_anchor = checkpoint.get("vf_fine_anchor")
        engine.vf_coarse_rays_checked = checkpoint.get("vf_coarse_rays_checked", [])
        # Phase 3 (iterative) progress state. Old binary-search fields
        # (chip_lo, chip_hi, chip_converged) in legacy checkpoints are
        # silently ignored — the iterative loop's state is stable_freq_arrays
        # itself, loaded above. chip_max defaults to None (re-initialized
        # by _phase3_profiling on next entry) when missing from legacy
        # checkpoints.
        engine.profiling_round = checkpoint.get("profiling_round", 0)
        engine.stillness_streak = checkpoint.get("stillness_streak", 0)
        engine.chips_stable_pct = checkpoint.get("chips_stable_pct", 0.0)
        engine.chip_max = checkpoint.get("chip_max")
        engine.phase3_active = checkpoint.get("phase3_active", False)
        engine.polish_round = checkpoint.get("polish_round", 0)
        engine.polish_active = checkpoint.get("polish_active", False)
        engine.stock_baseline = checkpoint.get("stock_baseline", engine.stock_baseline)
        engine.voltage_results = checkpoint.get("voltage_results", [])
        engine.wattage_results = checkpoint.get("wattage_results", [])
        engine.wattage_search_low = checkpoint.get("wattage_search_low")
        engine.wattage_search_high = checkpoint.get("wattage_search_high")
        engine.best_wattage_w = checkpoint.get("best_wattage_w")
        # Whatsminer (stock MicroBT) firmware state — additive load with
        # safe defaults for legacy checkpoints.
        engine.whatsminer_baselines = checkpoint.get("whatsminer_baselines")
        engine.whatsminer_freq_pct_anchor = checkpoint.get("whatsminer_freq_pct_anchor")
        engine.whatsminer_results = checkpoint.get("whatsminer_results", [])
        engine.whatsminer_pre_tune = checkpoint.get("whatsminer_pre_tune")
        engine.whatsminer_best_cell = checkpoint.get("whatsminer_best_cell")
        engine._wm_current_mode = checkpoint.get("_wm_current_mode")
        engine._wm_current_percent = checkpoint.get("_wm_current_percent")
        engine._wm_current_power_limit = checkpoint.get("_wm_current_power_limit")
        engine._wm_drift_streak = checkpoint.get("_wm_drift_streak", 0)
        engine.config_snapshot = sanitize(checkpoint.get("config_snapshot"))
        engine.current_step_started_at = checkpoint.get("current_step_started_at")
        # num_boards / chips_per_board were adopted above with the empty-array
        # fallbacks — don't re-read here or we'd clobber them on a checkpoint
        # missing the key (default 3 would overwrite a live-detected value).
        engine.parked_chips = [
            set(c) for c in checkpoint.get("parked_chips", [[] for _ in range(engine.num_boards)])
        ]
        # Clamp any sub-minimum frequencies from old checkpoints (firmware
        # minimum is 50 MHz; DEAD_CHIP_FREQ enforces this bound).
        dead_min = engine.config["DEAD_CHIP_FREQ"]
        for arr in engine.stable_freq_arrays:
            for i in range(len(arr)):
                if arr[i] < dead_min:
                    arr[i] = dead_min
        # Offline state — if a previous run was waiting for the miner to
        # come back and the process crashed, resume in that state so the
        # dashboard stays honest about what the tuner is doing.
        saved_phase = checkpoint.get("phase")
        if saved_phase == engine.PHASE_OFFLINE:
            engine.phase = engine.PHASE_OFFLINE
            engine.pre_offline_phase = checkpoint.get("pre_offline_phase")
            engine.pre_offline_phase_detail = checkpoint.get("pre_offline_phase_detail", "")
            engine.offline_since_ts = checkpoint.get("offline_since_ts")
        engine.last_successful_contact_ts = checkpoint.get("last_successful_contact_ts")
        engine.mrr_last_sync = checkpoint.get("mrr_last_sync")


def save_profile(engine):
    if engine._destroyed:
        return
    filepath = profile_path(engine)
    profile = {
        "ip": engine.ip,
        "mac": engine.mac,
        "firmware_type": engine.firmware_type,
        "min_voltage_mv": engine.min_voltage_mv,
        "num_boards": engine.num_boards,
        "chips_per_board": engine.chips_per_board,
        "baseline_scores": engine.baseline_scores,
        "baseline_chip_temps": engine.baseline_chip_temps,
        "baseline_chip_hashrates": engine.baseline_chip_hashrates,
        "baseline_freq_arrays": engine.baseline_freq_arrays,
        "stable_freq_arrays": engine.stable_freq_arrays,
        "stock_baseline": engine.stock_baseline,
        "best_efficiency": engine.best_efficiency,
        "voltage_results": engine.voltage_results,
        "wattage_results": engine.wattage_results,
        "wattage_search_low": engine.wattage_search_low,
        "wattage_search_high": engine.wattage_search_high,
        "best_wattage_w": engine.best_wattage_w,
        # Whatsminer (stock MicroBT) firmware state — see engine.__init__ for
        # field descriptions. Persisted so resume after a crash mid-pass picks
        # up the discovered baselines / anchor / current grid position.
        "whatsminer_baselines": engine.whatsminer_baselines,
        "whatsminer_freq_pct_anchor": engine.whatsminer_freq_pct_anchor,
        "whatsminer_results": engine.whatsminer_results,
        "whatsminer_pre_tune": engine.whatsminer_pre_tune,
        "whatsminer_best_cell": engine.whatsminer_best_cell,
        "_wm_current_mode": engine._wm_current_mode,
        "_wm_current_percent": engine._wm_current_percent,
        "_wm_current_power_limit": engine._wm_current_power_limit,
        "_wm_drift_streak": engine._wm_drift_streak,
        # Sole source of truth for the dynamic state machine. Top-K and
        # all "is X done" predicates are derived from this surface plus
        # voltage_results — no separate top-K list is persisted because
        # the ranking changes dynamically with TARGET_MODE / minerstat data.
        "vf_surface": engine.vf_surface,
        # Crash-safety for atomic chip-tune. None when monitor / tune
        # settled. stable_freq_arrays + profiling_round + stillness_streak
        # + polish_round (saved elsewhere) carry the iterative-loop resume
        # state.
        "in_flight_chip_tune_target": engine.in_flight_chip_tune_target,
        # Dashboard convenience — full planned coarse + fine grid for
        # rendering pending cells. Computed on-the-fly in get_status,
        # so persisting an empty list here is fine; the load path
        # silently ignores it via .get() defaults.
        "vf_planned_grid": engine.vf_planned_grid,
        "remeasure_queue": list(engine.remeasure_queue),
        "config_snapshot": sanitize(engine.config_snapshot),
        "active_sweep_voltage_mv": engine.active_sweep_voltage_mv,
        "voltage_adjustment_mv": engine.voltage_adjustment_mv,
        "last_restart_ts": engine.last_restart_ts,
        "mrr_last_sync": engine.mrr_last_sync,
        "saved_at": datetime.now().isoformat(),
    }
    _atomic_json_write(filepath, sanitize(profile))


def load_profile(engine):
    filepath = profile_path(engine)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath) as f:
            return sanitize(json.load(f))
    except Exception:
        return None


def save_checkpoint(engine):
    """Save intermediate tuning state so progress survives crashes.

    Schema: vf_surface + voltage_results are the authoritative state for
    the dynamic state machine. in_flight_chip_tune_target + stable_freq_arrays
    + profiling_round + stillness_streak + polish_round let a kill
    mid-chip-tune resume the iterative loop without re-running prior
    rounds. Everything else is supporting state (topology, baseline, MRR,
    offline, etc.)."""
    if engine._destroyed:
        return
    filepath = checkpoint_path(engine)
    checkpoint = {
        "ip": engine.ip,
        "mac": engine.mac,
        "firmware_type": engine.firmware_type,
        "min_voltage_mv": engine.min_voltage_mv,
        "start_voltage_mv": engine.start_voltage_mv,
        "psu_max_mv": engine.psu_max_mv,
        "baseline_scores": engine.baseline_scores,
        "baseline_chip_temps": engine.baseline_chip_temps,
        "baseline_chip_hashrates": engine.baseline_chip_hashrates,
        "baseline_freq_arrays": engine.baseline_freq_arrays,
        "stable_freq_arrays": engine.stable_freq_arrays,
        # Authoritative surface — every measurement, every iteration of
        # the dynamic loop derives top-K / "is X done" / etc. from this
        # plus voltage_results.
        "vf_surface": engine.vf_surface,
        "vf_planned_grid": engine.vf_planned_grid,
        "remeasure_queue": list(engine.remeasure_queue),
        # Atomic-chip-tune resume marker. None when monitor or no chip-tune
        # in flight.
        "in_flight_chip_tune_target": engine.in_flight_chip_tune_target,
        # Phase 3 (iterative) progress counters — used by the dashboard
        # while in flight, and reloaded so a mid-loop crash resumes near
        # where it left off. The iterative loop's per-chip frequency state
        # IS stable_freq_arrays (saved above); these fields are just the
        # round counter, stillness streak, and live percentage. chip_max
        # is the per-chip "lowest known-unstable freq" memory — also
        # required for resume so the post-crash loop doesn't re-test
        # frequencies the pre-crash loop already proved unstable.
        "profiling_round": engine.profiling_round,
        "stillness_streak": engine.stillness_streak,
        "chips_stable_pct": engine.chips_stable_pct,
        "chip_max": engine.chip_max,
        "phase3_active": engine.phase3_active,
        "polish_round": engine.polish_round,
        "polish_active": engine.polish_active,
        "stock_baseline": engine.stock_baseline,
        "voltage_results": engine.voltage_results,
        "wattage_results": engine.wattage_results,
        "wattage_search_low": engine.wattage_search_low,
        "wattage_search_high": engine.wattage_search_high,
        "best_wattage_w": engine.best_wattage_w,
        # Whatsminer (stock MicroBT) firmware state — additive checkpoint
        # serialization. Restore via .get() with safe defaults so legacy
        # checkpoints still parse.
        "whatsminer_baselines": engine.whatsminer_baselines,
        "whatsminer_freq_pct_anchor": engine.whatsminer_freq_pct_anchor,
        "whatsminer_results": engine.whatsminer_results,
        "whatsminer_pre_tune": engine.whatsminer_pre_tune,
        "whatsminer_best_cell": engine.whatsminer_best_cell,
        "_wm_current_mode": engine._wm_current_mode,
        "_wm_current_percent": engine._wm_current_percent,
        "_wm_current_power_limit": engine._wm_current_power_limit,
        "_wm_drift_streak": engine._wm_drift_streak,
        "config_snapshot": sanitize(engine.config_snapshot),
        "current_step_started_at": engine.current_step_started_at,
        "num_boards": engine.num_boards,
        "chips_per_board": engine.chips_per_board,
        "parked_chips": [sorted(s) for s in engine.parked_chips],
        "phase": engine.phase,
        "pre_offline_phase": engine.pre_offline_phase,
        "pre_offline_phase_detail": engine.pre_offline_phase_detail,
        "offline_since_ts": engine.offline_since_ts,
        "last_successful_contact_ts": engine.last_successful_contact_ts,
        "mrr_last_sync": engine.mrr_last_sync,
        "saved_at": datetime.now().isoformat(),
    }
    _atomic_json_write(filepath, sanitize(checkpoint))
    engine.log(
        f"Checkpoint saved (vf_surface {len(engine.vf_surface)} points, "
        f"voltage_results {len(engine.voltage_results)} entries, "
        f"in_flight={'yes' if engine.in_flight_chip_tune_target else 'no'})"
    )


def load_checkpoint(engine):
    filepath = checkpoint_path(engine)
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath) as f:
            return sanitize(json.load(f))
    except Exception:
        return None
