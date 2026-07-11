"""
Dashboard-feeding status derivation utilities for the tuning engine.
Provides methods to compute average temperatures, top tunes, planned grids,
and derive status data for dashboard consumption.
"""

from __future__ import annotations

from datetime import datetime

from tuner_app.miner.exceptions import MinerOfflineError
from tuner_app.mrr.rental_cache import rental_cache
from tuner_app.privacy import sanitize
from tuner_app.profit.compute import compute_profit_usd_per_day, score_cell
from tuner_app.tuning_engine.phases import (
    PHASE_ERROR,
    PHASE_IDLE,
    PHASE_OFFLINE,
    PHASE_PERPETUAL,
    PHASE_STOPPED,
)


def compute_tuner_bucket(phase: str, engine_busy: bool) -> str:
    """Derive the dashboard tuner-status bucket from the engine's phase
    and thread-liveness signal.

    Returns one of: "idle", "tuning", "maintaining", "offline", "error",
    "stopped", "stopping". The "stopping" bucket is the post-Stop
    wind-down window (PHASE_STOPPED + alive thread) so the dashboard can
    show the operator that the worker is still cleaning up. The
    orphan-thread case (any non-terminal phase + dead thread) normalizes
    to "stopped" to avoid stale "profiling"/"maintaining" indicators
    when the worker thread has crashed or been reaped.
    """
    if phase == PHASE_IDLE:
        return "idle"
    if phase == PHASE_ERROR:
        return "error"
    if phase == PHASE_STOPPED:
        return "stopping" if engine_busy else "stopped"
    # Non-terminal phase + dead thread = orphan/crashed exit; normalize to stopped.
    if not engine_busy:
        return "stopped"
    if phase == PHASE_OFFLINE:
        return "offline"
    if phase == PHASE_PERPETUAL:
        return "maintaining"
    return "tuning"


def compute_avg_temps_c(engine):
    def _avg_board_temps(boards):
        if not boards:
            return None
        flat = [
            t
            for b in boards
            for t in [b.temp_inlet_c, b.temp_outlet_c]
            if isinstance(t, (int, float))
        ]
        return sum(flat) / len(flat) if flat else None

    def _avg_chip_temps(boards):
        if not boards:
            return None
        flat = [t for b in boards for t in b.chip_temps_c if isinstance(t, (int, float))]
        return sum(flat) / len(flat) if flat else None

    return _avg_board_temps(engine.last_temps), _avg_chip_temps(engine.last_chip_temps)


def compute_top_tunes(engine, limit=3):
    """R7: Union of coarse-surface / fine-surface / chip-tuned readings,
    ranked by J/TH. Deduped by voltage — at most one entry per voltage in
    the output, with chip-tuned preferred over coarse/fine when both exist
    at the same voltage. Returns up to `limit` entries, shaped for the
    dashboard's Highest Efficiency/Profit Tunes table.

    Each returned entry carries BOTH `efficiency_jth` and
    `profit_usd_day` (when profit data is computable) so the frontend
    can render a dual-column view regardless of which column is the
    active ranking target. The active target mode is also stamped as
    `target_mode` on each entry so the dashboard knows which column to
    emphasize without re-fetching the engine config.

    Ranking uses the active target mode's score. Dedup-within-voltage
    picks the best-scoring entry under the active mode."""
    ctx = engine._get_scoring_context()
    target_mode = ctx[0]
    display_rate, display_coin_data, display_modifier = engine._get_profit_display_context()
    candidates = []

    def profit_for(entry):
        """Compute $/day for an entry using the display context's coin
        data, which is populated whenever minerstat has a snapshot for
        the configured coin — independent of TARGET_MODE. This is why
        efficiency-mode top_tunes rows still surface $/day: ranking stays
        on J/TH (via ctx), but the column is populated off of display
        data. Returns None when no coin snapshot exists."""
        if display_coin_data is None:
            return None
        p = compute_profit_usd_per_day(
            entry.get("hashrate_ths"),
            entry.get("power_w"),
            display_coin_data,
            display_rate,
            display_modifier,
        )
        return p

    for r in engine.voltage_results or []:
        if r.get("efficiency_jth") is None:
            continue
        profit = profit_for(r)
        candidates.append(
            {
                "source": "chip-tuned",
                "voltage_mv": int(r["voltage_mv"]),
                "freq_mhz": r.get("avg_freq_mhz"),
                "hashrate_ths": r.get("hashrate_ths"),
                "power_w": r.get("power_w"),
                "efficiency_jth": float(r["efficiency_jth"]),
                "profit_usd_day": profit,
                "has_chip_tune": True,
                "can_retune": True,
                "retuned": bool(r.get("retuned")),
                "measured_at": r.get("measured_at"),
                "target_mode": target_mode,
            }
        )
    for e in engine.vf_surface or []:
        if e.get("efficiency_jth") is None:
            continue
        profit = profit_for(e)
        candidates.append(
            {
                "source": "fine" if e.get("fine") else "coarse",
                "voltage_mv": int(e["voltage_mv"]),
                "freq_mhz": round(float(e["freq_mhz"]), 3),
                "hashrate_ths": e.get("hashrate_ths"),
                "power_w": e.get("power_w"),
                "efficiency_jth": float(e["efficiency_jth"]),
                "profit_usd_day": profit,
                "has_chip_tune": False,
                "can_retune": True,
                "retuned": False,
                "measured_at": e.get("measured_at"),
                "coarse_anchor": e.get("coarse_anchor"),
                "target_mode": target_mode,
            }
        )

    # Dedup by voltage: prefer chip-tuned; among same-category entries at
    # the same voltage, keep the best-scoring one under the active mode.
    def rank_key(c):
        s = score_cell(c, *ctx)
        return float("inf") if s is None else s

    by_v = {}
    for c in candidates:
        v = c["voltage_mv"]
        existing = by_v.get(v)
        if existing is None:  # noqa: SIM114
            by_v[v] = c
        elif c["has_chip_tune"] and not existing["has_chip_tune"]:  # noqa: SIM114
            by_v[v] = c
        elif existing["has_chip_tune"] == c["has_chip_tune"] and rank_key(c) < rank_key(existing):
            by_v[v] = c
    ranked = sorted(by_v.values(), key=rank_key)
    return ranked[:limit]


def _derive_whatsminer_planned_grid(engine):
    """Whatsminer planned-grid emitter — `power_limit_W × target_freq_MHz`.

    Cell shape mirrors `whatsminer_phases._measure_pl_freq_cell`'s output
    (sans the measurement fields), so the frontend's measured-vs-planned
    cell-key match works uniformly:
        {power_limit_w, target_freq_mhz, voltage_mv: None, freq_mhz,
         axis_x_kind: "power_limit_w", fine: False}

    Returns [] if either axis collapses to <2 points (chart would be
    degenerate). Fine sub-cell hinting is intentionally omitted — the
    Whatsminer fine grid lives around top-K coarse anchors picked at
    runtime, and emitting planned fine sub-cells upfront would draw a
    densely-pending heatmap that misrepresents the engine's actual
    fine-pass ordering. Real fine measurements still render once they
    land in vf_surface."""
    # Local import keeps status.py independent of whatsminer_grid at
    # module-import time; the function is invoked rarely (per dashboard
    # poll cycle) so the import-cost is negligible.
    from tuner_app.tuning_engine.whatsminer_grid import (
        build_freq_axis,
        build_power_limit_axis,
    )

    try:
        pl_axis = build_power_limit_axis(engine)
        f_axis = build_freq_axis(engine)
    except Exception:
        return []
    if len(pl_axis) < 2 or len(f_axis) < 2:
        return []
    planned = []
    for pl in pl_axis:
        for f in f_axis:
            planned.append(
                {
                    "power_limit_w": int(pl),
                    "target_freq_mhz": round(float(f), 3),
                    "voltage_mv": None,
                    "freq_mhz": round(float(f), 3),
                    "axis_x_kind": "power_limit_w",
                    "fine": False,
                }
            )
    return planned


def derive_planned_grid_for_dashboard(engine):
    """Build the full coarse + (top-fine-anchor's) fine planned grid
    for the dashboard heatmap. Pure-derived from current settings, so
    the dashboard always sees the grid that would be measured next under
    current config — no stale pending cells if operator widens F_MAX.

    Fine planned cells are gated on coarse work being settled. The
    dashboard renders fine planned cells as dashed sub-cells inside the
    owning coarse cell; emitting them while we're still walking coarse
    rays makes the top-K coarse anchors look like they've been converted
    to a fine sub-grid (the "after reset, coarse cells display as fine
    grid cells" complaint). Mirror the engine's actual phase ordering:
    fine planned cells only enter the wire format once
    _find_next_coarse_to_measure returns None. Fine measurements that
    already exist in vf_surface render unconditionally — the gate only
    affects the planned/pending sub-cell overlay.

    Whatsminer (`power_limit_freq_search` strategy) doesn't sweep voltage —
    its coarse grid is wattage × target_freq. We short-circuit early to
    `_derive_whatsminer_planned_grid` so the dashboard's Y-axis shows the
    actual axis the engine sweeps."""
    try:
        if engine.api.tuning_strategy() == "power_limit_freq_search":
            return _derive_whatsminer_planned_grid(engine)
    except Exception:
        # If api or tuning_strategy is missing (test fixtures, partial init),
        # fall through to the legacy voltage-grid emitter.
        pass

    try:
        v_grid_asc, f_grid_asc = engine._vf_grid_axes()
    except Exception:
        return []
    if len(v_grid_asc) < 2 or len(f_grid_asc) < 2:
        return []
    planned = [
        {"voltage_mv": int(v), "freq_mhz": round(float(f), 3), "fine": False}
        for v in v_grid_asc
        for f in f_grid_asc
    ]
    fine_count = int(engine.config.get("VF_EXPLORE_FINE_COUNT", 0))
    if fine_count >= 2:
        try:
            coarse_pending = engine._find_next_coarse_to_measure() is not None
        except Exception:
            coarse_pending = False
        if coarse_pending:
            return planned
        fine_top_k = max(1, int(engine.config.get("VF_FINE_TOP_K", 3)))
        try:
            score_ctx = engine._get_scoring_context()
            top_anchors = engine._top_fine_anchors(score_ctx, fine_top_k)
        except Exception:
            top_anchors = []
        v_step = (v_grid_asc[-1] - v_grid_asc[0]) / max(1, len(v_grid_asc) - 1)
        f_step = (f_grid_asc[-1] - f_grid_asc[0]) / max(1, len(f_grid_asc) - 1)
        v_min = float(v_grid_asc[0])
        v_max = float(v_grid_asc[-1])
        f_min = float(engine.config["VF_EXPLORE_F_MIN"])
        f_max = float(engine.config["VF_EXPLORE_F_MAX"])
        seen = {(p["voltage_mv"], round(float(p["freq_mhz"]), 3)): True for p in planned}
        for anchor in top_anchors:
            anchor_v = int(anchor["voltage_mv"])
            anchor_f = round(float(anchor["freq_mhz"]), 3)
            v_fine, f_fine = engine._fine_cell_offsets_for_anchor(
                anchor_v, anchor_f, v_step, f_step, fine_count, v_min, v_max, f_min, f_max
            )
            if not v_fine or not f_fine:
                continue
            anchor_meta = {"voltage_mv": anchor_v, "freq_mhz": anchor_f}
            for v in v_fine:
                for f in f_fine:
                    k = (int(v), round(float(f), 3))
                    if k in seen:
                        continue
                    seen[k] = True
                    planned.append(
                        {
                            "voltage_mv": int(v),
                            "freq_mhz": round(float(f), 3),
                            "fine": True,
                            "coarse_anchor": dict(anchor_meta),
                        }
                    )
    return planned


def derive_top_k_for_dashboard(engine):
    """Compute top-K display state for the dashboard. Mirrors
    _find_next_chip_tune_target's candidate selection so the heatmap's
    gold/blue borders match what the engine would actually chip-tune next.

    Returns a list of {voltage_mv, seed_f_mhz, coarse_jth, vf_source}
    descriptors — same shape as the legacy vf_top_k_voltages list, so the
    dashboard's existing renderers continue to work unchanged. Also
    returns the index into that list of the in-flight chip-tune (or
    None) to fill the legacy vf_refinement_index slot."""
    chip_top_k = max(1, int(engine.config.get("VF_EXPLORE_TOP_K", 1)))
    fine_top_k = max(1, int(engine.config.get("VF_FINE_TOP_K", 3)))
    # Defensive clamp: chip-tune candidates come from inside the top-fine_top_k
    # anchors' fine grids. Operator may set EXPLORE_TOP_K > FINE_TOP_K (validator
    # no longer rejects); keep the documented invariant chip_top_k <= fine_top_k.
    chip_top_k = min(chip_top_k, fine_top_k)
    fine_count = int(engine.config.get("VF_EXPLORE_FINE_COUNT", 0))
    try:
        score_ctx = engine._get_scoring_context()
        coarse_ranked = engine._coarse_cells_ranked(score_ctx)
    except Exception:
        return [], None
    if fine_count < 2:
        if not coarse_ranked:
            return [], None
        candidates = coarse_ranked[:chip_top_k]
    else:
        # Anchor list includes in-flight (converted) grids + new top-K
        # coarse. Same source as `_find_next_chip_tune_target` so the
        # dashboard's gold/blue borders match what the engine picks.
        anchor_keys = {
            (int(a["voltage_mv"]), round(float(a["freq_mhz"]), 3))
            for a in engine._top_fine_anchors(score_ctx, fine_top_k)
        }
        fine_pool = []
        for e in engine.vf_surface:
            if not e.get("fine"):
                continue
            if score_cell(e, *score_ctx) is None:
                continue
            anc = e.get("coarse_anchor") or {}
            ak = (int(anc.get("voltage_mv", -1)), round(float(anc.get("freq_mhz", -1)), 3))
            if ak not in anchor_keys:
                continue
            fine_pool.append(e)
        sorted_pool = sorted(fine_pool, key=lambda e: score_cell(e, *score_ctx))
        candidates = sorted_pool[:chip_top_k]
    descriptors = []
    for cell in candidates:
        descriptors.append(
            {
                "voltage_mv": int(cell["voltage_mv"]),
                "seed_f_mhz": round(float(cell["freq_mhz"]), 3),
                "coarse_jth": float(cell.get("efficiency_jth", 0) or 0),
                # Legacy flag fields — kept True so dashboard renderers that
                # check `tk.coarse_rays_walked` / `tk.fine_gridded` don't gate
                # rendering. The dynamic state machine's else-if structure
                # makes these always-true conceptually: by the time something
                # is in the chip-tune candidate pool, the rays + fine grids
                # for its anchor are already complete (the loop blocks on it).
                "coarse_rays_walked": True,
                "fine_gridded": True,
                "vf_source": {
                    "kind": "fine" if cell.get("fine") else "coarse",
                    "voltage_mv": int(cell["voltage_mv"]),
                    "freq_mhz": round(float(cell["freq_mhz"]), 3),
                    "coarse_jth": float(cell.get("efficiency_jth", 0) or 0),
                    "hashrate_ths": cell.get("hashrate_ths"),
                    "power_w": cell.get("power_w"),
                },
            }
        )
    # Map in-flight chip-tune target to its index in descriptors (for the
    # dashboard's "currently chip-tuning #N" badge).
    refinement_idx = None
    if engine.in_flight_chip_tune_target is not None:
        tgt = engine.in_flight_chip_tune_target
        tol = float(engine.config.get("FREQ_SEARCH_TOLERANCE_MHZ", 7))
        for i, d in enumerate(descriptors):
            if (
                d["voltage_mv"] == int(tgt.get("voltage_mv", -1))
                and abs(d["seed_f_mhz"] - float(tgt.get("freq_mhz", 0))) <= tol
            ):
                refinement_idx = i
                break
    return descriptors, refinement_idx


def get_status(engine):
    tuned_stats = {}
    if engine.last_summary:
        power = engine.last_summary.power_w
        ths = engine.last_summary.hashrate_ths
        tuned_stats = {
            "hashrate_ths": ths,
            "power_w": power,
            "efficiency_jth": power / ths if ths > 0 else 0,
            "state": engine.last_summary.operating_state,
            "voltage_mv": engine.last_summary.target_voltage_mv or 0,
            "fan_speed": engine.last_summary.fan_speed,
        }
    # Derive top-K + refinement index from vf_surface ranking so the
    # dashboard sees what the engine WOULD chip-tune next under the
    # current TARGET_MODE / VF_*_TOP_K settings — keeping the heatmap
    # and Best Tunes card in sync with the dynamic state machine.
    derived_top_k, derived_refinement_idx = engine._derive_top_k_for_dashboard()
    avg_board_temp_c, avg_chip_temp_c = engine._compute_avg_temps_c()
    engine_busy = bool(engine.thread and engine.thread.is_alive())
    return sanitize(
        {
            "ip": engine.ip,
            "phase": engine.phase,
            "phase_detail": engine.phase_detail,
            "profiling_round": engine.profiling_round,
            "profiling_completion_pct": engine.profiling_completion_pct,
            "chips_stable_pct": engine.chips_stable_pct,
            "chips_converged": engine.chips_converged,
            "chips_alive": engine.chips_alive,
            "stillness_streak": engine.stillness_streak,
            "polish_round": engine.polish_round,
            "polish_active": engine.polish_active,
            "current_step_voltage_mv": engine.min_voltage_mv,
            "current_step_started_at": engine.current_step_started_at,
            "tuning_complete": engine.tuning_complete,
            "stock_baseline": engine.stock_baseline,
            "tuned_stats": tuned_stats,
            "best_efficiency": engine.best_efficiency,
            "stable_freq_arrays": engine.stable_freq_arrays,
            "baseline_scores": engine.baseline_scores,
            # Per-chip Phase 2 captures alongside baseline_scores. Surfaced to
            # the dashboard's right-hand "Phase 2 Baseline" heatmap pane (Freq /
            # Health / Chip Temp / Hashrate tabs). Populated at the end of
            # _phase2_baseline; preserved across retunes (baseline isn't
            # re-collected on retune).
            "baseline_chip_temps": engine.baseline_chip_temps,
            "baseline_chip_hashrates": engine.baseline_chip_hashrates,
            "baseline_freq_arrays": engine.baseline_freq_arrays,
            "voltage_results": engine.voltage_results,
            "vf_surface": engine.vf_surface,
            # Top-K is derived on every status call from current vf_surface
            # ranking + current settings. The dashboard's heatmap winner /
            # top-K border code reads this; staying compatible with the legacy
            # field name lets dashboard.html stay mostly unchanged.
            "vf_top_k_voltages": derived_top_k,
            "vf_refinement_index": derived_refinement_idx,
            "in_flight_chip_tune_target": engine.in_flight_chip_tune_target,
            # Planned grid derived from current config every status call so
            # the dashboard's "pending cell" rendering stays in sync with
            # operator config changes without waiting for the next tune.
            "vf_planned_grid": engine._derive_planned_grid_for_dashboard(),
            # vf_skipped is now empty — the dynamic state machine doesn't
            # track skipped cells (a "skipped" cell is just one nothing has
            # found yet; if ranking shifts, it could become next-to-measure).
            # Kept in the response for dashboard backward compat.
            "vf_skipped": [],
            "remeasure_queue": list(engine.remeasure_queue),
            "current_vf_point": engine.current_vf_point,
            "num_boards": engine.num_boards,
            "chips_per_board": engine.chips_per_board,
            "active_sweep_voltage_mv": engine.active_sweep_voltage_mv,
            "sweep_voltage_mv": engine.sweep_voltage_mv,
            "sweep_hashrate_ths": engine.sweep_hashrate_ths,
            "voltage_adjustment_mv": engine.voltage_adjustment_mv,
            "last_restart_ts": engine.last_restart_ts,
            "offline_since_ts": engine.offline_since_ts,
            "offline_failure_count": engine.offline_failure_count,
            "last_successful_contact_ts": engine.last_successful_contact_ts,
            "pre_offline_phase": engine.pre_offline_phase,
            "engine_busy": engine_busy,
            "tuner_bucket": compute_tuner_bucket(engine.phase, engine_busy),
            "mrr_last_sync": engine.mrr_last_sync,
            "mrr_rental_status": rental_cache.get(engine.mac),
            "firmware_type": engine.firmware_type,
            "capabilities": {
                "supports_per_chip_tuning": engine.api.supports_per_chip_tuning(),
                "has_external_power_limit": engine.api.has_external_power_limit(),
                "has_capabilities_endpoint": engine.api.has_capabilities_endpoint(),
                "has_internal_perpetual_tune": engine.api.has_internal_perpetual_tune(),
                "wattage_search_strategy": engine.api.tuning_strategy() == "wattage_search",
                "voltage_chip_tune_strategy": engine.api.tuning_strategy() == "voltage_chip_tune",
                "power_limit_freq_search_strategy": (
                    engine.api.tuning_strategy() == "power_limit_freq_search"
                ),
            },
            "config": {
                "min_voltage_mv": engine.min_voltage_mv,
                "freq_search_tolerance_mhz": engine.config["FREQ_SEARCH_TOLERANCE_MHZ"],
                "chip_freq_spread_mhz": engine.config["CHIP_FREQ_SPREAD_MHZ"],
                "vf_explore_trend_confirm": engine.config["VF_EXPLORE_TREND_CONFIRM"],
                "vf_explore_wait": engine.config["VF_EXPLORE_WAIT"],
                "vf_explore_samples": engine.config["VF_EXPLORE_SAMPLES"],
                "vf_explore_sample_interval": engine.config["VF_EXPLORE_SAMPLE_INTERVAL"],
                "vf_explore_v_count": engine.config["VF_EXPLORE_V_COUNT"],
                "vf_explore_f_count": engine.config["VF_EXPLORE_F_COUNT"],
                "vf_explore_top_k": int(engine.config.get("VF_EXPLORE_TOP_K", 1)),
                "vf_fine_top_k": int(engine.config.get("VF_FINE_TOP_K", 3)),
                "vf_coarse_top_k_rays": engine.config.get("VF_COARSE_TOP_K_RAYS", 1),
                "chip_tune_step_mhz": engine.config.get("CHIP_TUNE_STEP_MHZ", 6.25),
                "chip_tune_up_tolerance": int(engine.config.get("CHIP_TUNE_UP_TOLERANCE", 5)),
                "chip_tune_down_tolerance": int(engine.config.get("CHIP_TUNE_DOWN_TOLERANCE", 15)),
                "chip_tune_stillness_streak": int(
                    engine.config.get("CHIP_TUNE_STILLNESS_STREAK", 2)
                ),
                "max_profiling_rounds": int(engine.config.get("MAX_PROFILING_ROUNDS", 60)),
                "stability_polish_rounds": engine.config.get("STABILITY_POLISH_ROUNDS", 3),
                "stability_polish_step_mhz": engine.config.get("STABILITY_POLISH_STEP_MHZ", 6.25),
                "stability_polish_round_samples": int(
                    engine.config.get("STABILITY_POLISH_ROUND_SAMPLES", 40)
                ),
                "stability_polish_round_interval": int(
                    engine.config.get("STABILITY_POLISH_ROUND_INTERVAL", 30)
                ),
                "target_mode": engine.config.get("TARGET_MODE", "efficiency"),
                "electric_rate_per_kwh": engine.config.get("ELECTRIC_RATE_PER_KWH", 0.10),
                "minerstat_coin": engine.config.get("MINERSTAT_COIN", "BTC"),
                "income_modifier_pct": engine.config.get("INCOME_MODIFIER_PCT", 0.0),
                "mrr_enabled": bool(engine.config.get("MRR_ENABLED", False)),
                "mrr_rig_id": int(engine.config.get("MRR_RIG_ID", 0) or 0),
                "mrr_hashrate_modifier_pct": float(
                    engine.config.get("MRR_HASHRATE_MODIFIER_PCT", 0.0) or 0.0
                ),
                "mrr_hashrate_unit": str(engine.config.get("MRR_HASHRATE_UNIT", "th") or "th"),
            },
            "top_tunes": engine._compute_top_tunes(),
            "avg_board_temp_c": avg_board_temp_c,
            "avg_chip_temp_c": avg_chip_temp_c,
        }
    )


def get_live_data(engine):
    # HTTP handlers call this per-request; an offline miner shouldn't
    # 500 the endpoint. Return last-known values instead, letting the
    # dashboard render them with the offline banner.
    try:  # noqa: SIM105
        engine._update_live_data()
    except MinerOfflineError:
        pass
    return sanitize(
        {
            "hashrate": engine.last_hashrate,
            "clocks": engine.last_clocks,
            "temps": engine.last_temps,
            "chip_temps": engine.last_chip_temps,
            "summary": engine.last_summary,
        }
    )


def get_export(engine, current_config=None):
    """Full export bundle: stock vs tuned, the full Phase V V/F surface, every
    top-K voltage_results entry with per-board data, tuned freqs, baseline
    scores, and the config used at tune time plus the currently-live config
    for diff."""
    status = engine.get_status()
    return sanitize(
        {
            "ip": engine.ip,
            "exported_at": datetime.now().isoformat(),
            "phase": engine.phase,
            "tuning_complete": engine.tuning_complete,
            "stock_baseline": engine.stock_baseline,
            "tuned_stats": status["tuned_stats"],
            "best_efficiency": engine.best_efficiency,
            "voltage_results": engine.voltage_results,
            "vf_surface": engine.vf_surface,
            "vf_top_k_voltages": engine.vf_top_k_voltages,
            "stable_freq_arrays": engine.stable_freq_arrays,
            "baseline_scores": engine.baseline_scores,
            "num_boards": engine.num_boards,
            "chips_per_board": engine.chips_per_board,
            "min_voltage_mv": engine.min_voltage_mv,
            "psu_max_mv": engine.psu_max_mv,
            "config_used": sanitize(engine.config_snapshot),
            "config_current": dict(current_config) if current_config is not None else None,
        }
    )
