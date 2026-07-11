"""
Phase V orchestrator. Samples a coarse (V, F_uniform) grid,
optionally a fine grid around the coarse peak, and populates
self.vf_top_k_voltages with the per-chip refinement targets. Resume-safe:
points already present in self.vf_surface are skipped on reentry.
"""

from __future__ import annotations

from tuner_app.miner.exceptions import MinerCommandError
from tuner_app.profit.compute import compute_profit_usd_per_day, score_cell


def phase_vf_exploration(engine):
    engine.phase = engine.PHASE_VF_EXPLORATION
    engine.phase_detail = "Phase V: initializing 2D efficiency exploration"
    # Drain any queued remeasurements before starting the ray walk so
    # the trend-confirm logic sees up-to-date J/TH at the re-measured
    # cells. Safe no-op when the queue is empty.
    engine._drain_remeasure_queue()
    if not engine.running:
        return

    v_grid = engine._build_vf_grid_voltages()
    f_grid = engine._build_vf_grid_freqs()
    # Normalize to ascending so V+ and F+ ray steps mean "higher V" /
    # "higher F" regardless of the build order.
    v_grid_asc = sorted(set(int(v) for v in v_grid))
    f_grid_asc = sorted({round(float(f), 3) for f in f_grid})
    if len(v_grid_asc) < 2 or len(f_grid_asc) < 2:
        raise MinerCommandError(
            f"Phase V grid too small: {len(v_grid_asc)} V × {len(f_grid_asc)} F"
        )

    # Publish the full planned grid up front so the dashboard can render
    # every cell as pending before the first measurement lands. Cells the
    # 8-ray walk never reaches (from the converged best center) are moved
    # to vf_skipped in one pass after convergence. Rebuilt each entry so
    # operator knob changes reflect in the plan rather than a stale snapshot.
    engine.vf_planned_grid = [
        {"voltage_mv": int(v), "freq_mhz": round(float(f), 3), "fine": False}
        for v in v_grid_asc
        for f in f_grid_asc
    ]
    trend_n = max(1, int(engine.config.get("VF_EXPLORE_TREND_CONFIRM", 2)))
    total_points = len(v_grid_asc) * len(f_grid_asc)
    engine.log("")
    engine.log(
        f"=== Phase V: 2D (V, F) re-spawn grid — {len(v_grid_asc)} voltages × "
        f"{len(f_grid_asc)} freqs = {total_points} points max ==="
    )
    engine.log(f"  V grid: {sorted(v_grid_asc, reverse=True)} mV")
    engine.log(f"  F grid: {f_grid_asc} MHz")
    engine.log(f"  trend-confirm N = {trend_n}")

    # Measured points indexed by (voltage_mv, round(freq_mhz, 3)) — includes
    # both coarse and fine entries, so resume replays the 8-ray walk from
    # the current best without re-measurement.
    measured = {
        (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)): e for e in engine.vf_surface
    }

    def grid_point(v_idx, f_idx):
        return int(v_grid_asc[v_idx]), float(f_grid_asc[f_idx])

    def measure_or_reuse(v_idx, f_idx, fine=False):
        """Measure (or reuse from `measured`) the grid point at the given
        indices. Appends to self.vf_surface + updates `measured` on a fresh
        measurement. Returns the entry, or None if engine was stopped.

        Callers that need to distinguish fresh measurements from cache
        hits (to suppress replay-noise logging) can set
        `measure_or_reuse._was_measured` before calling and check it
        after — the attribute is set True on fresh measurement, False
        on cache hit."""
        v_mv, f_mhz = grid_point(v_idx, f_idx)
        key = (int(v_mv), round(float(f_mhz), 3))
        if key in measured:
            measure_or_reuse._was_measured = False
            return measured[key]
        result = engine._measure_vf_point(v_mv, f_mhz, fine=fine)
        if result is None:
            measure_or_reuse._was_measured = False
            return None
        engine.vf_surface.append(result)
        measured[key] = result
        engine._save_checkpoint()
        measure_or_reuse._was_measured = True
        return result

    measure_or_reuse._was_measured = False

    # Scoring context — captured once per Phase V call. Drives every
    # ranking comparison (current_best, ray trend-stop, R5 top-K candidates,
    # pre-fine top-K selection, post-fine seed_f refinement). Log output
    # reads the entry's efficiency_jth directly for display; this only
    # governs ranking.
    score_ctx = engine._get_scoring_context()

    def score_of(entry):
        s = score_cell(entry, *score_ctx)
        return None if s is None else s

    def format_metric(entry):
        """Format an entry's primary metric for log display. In profit
        mode we show both J/TH and $/day so operators can see how the
        ranking decision was made. None-safe — returns '—' if the entry
        has no measurement data."""
        if entry is None:
            return "—"
        jth = entry.get("efficiency_jth")
        if jth is None:
            return "—"
        if score_ctx[0] == "profitability" and score_ctx[2] is not None:
            profit = compute_profit_usd_per_day(
                entry.get("hashrate_ths"),
                entry.get("power_w"),
                score_ctx[2],
                score_ctx[1],
                score_ctx[3],
            )
            if profit is not None:
                return f"{jth:.2f} J/TH, ${profit:.2f}/day"
        return f"{jth:.2f} J/TH"

    # ---- Step 1: seed center (or pick up current best from vf_surface) ----
    v_center_idx = len(v_grid_asc) // 2
    f_center_idx = len(f_grid_asc) // 2
    center = measure_or_reuse(v_center_idx, f_center_idx)
    if center is None:
        return

    def current_best():
        """Return (v_idx, f_idx, best_score, best_entry) of the best-scoring
        entry in vf_surface, or the center cell's indices with None score
        if nothing has measurement data yet. Derived each outer-loop
        iteration so resume picks up from the right center without needing
        an extra checkpoint field — the best point is always recoverable
        from vf_surface.

        "Best" means lowest score via score_of — which is efficiency_jth
        in efficiency mode, -profit_usd_day in profit mode. Log output
        reads best_entry["efficiency_jth"] for display regardless of mode."""
        scorable = [(score_of(e), e) for e in engine.vf_surface]
        scorable = [(s, e) for s, e in scorable if s is not None]
        if not scorable:
            return v_center_idx, f_center_idx, None, None
        scorable.sort(key=lambda pair: pair[0])
        best_score, best_entry = scorable[0]
        v_mv = int(best_entry["voltage_mv"])
        f_mhz = round(float(best_entry["freq_mhz"]), 3)
        try:
            vi = v_grid_asc.index(v_mv)
            fi = f_grid_asc.index(f_mhz)
        except ValueError:
            # Off-grid entry (e.g. fine cell from an older run); fall
            # back to center but report the winning score so the walk
            # re-spawns from any better coarse cell it later discovers.
            return v_center_idx, f_center_idx, best_score, best_entry
        return vi, fi, best_score, best_entry

    engine.log("")
    engine.log(
        f"Phase V: center at {v_grid_asc[v_center_idx]} mV / "
        f"{f_grid_asc[f_center_idx]:.1f} MHz ({format_metric(center)})"
    )
    if score_ctx[0] == "profitability":
        engine.log(
            f"Phase V: ranking by profitability ($/day) with rate "
            f"${score_ctx[1]:.4f}/kWh, coin {engine.config.get('MINERSTAT_COIN', 'BTC')}"
        )

    # ---- Step 2: mid-pass re-spawn 8-ray walk, optional top-K-cell ray
    # re-checks, then fine grid each of the top-K coarse cells sequentially
    # before any chip-tune runs. Ridge-first direction order (NE, SW first)
    # matches the V/F stability ridge where the best efficiencies tend to
    # live.
    #
    # Resume: on reentry, `current_best()` re-derives the best cell from
    # vf_surface, so a crash mid-pass restarts from whatever the best is
    # at load time. `measure_or_reuse` dedupes measured cells. Per-top-K
    # fine_gridded flags on `vf_top_k_voltages` entries let a crash
    # mid-fine-grid-loop resume at the next unfinished target.

    RAY_DIRECTIONS = (
        ("V+F+", +1, +1),  # NE — stability ridge
        ("V-F-", -1, -1),  # SW — stability ridge
        ("V+", +1, 0),
        ("V-", -1, 0),
        ("F+", 0, +1),
        ("F-", 0, -1),
        ("V+F-", +1, -1),  # NW — off-ridge
        ("V-F+", -1, +1),  # SE — off-ridge
    )

    max_passes = len(v_grid_asc) * len(f_grid_asc)  # pathological safety cap
    top_k_rays_cfg = max(1, int(engine.config.get("VF_COARSE_TOP_K_RAYS", 1)))
    fine_count_cfg = int(engine.config.get("VF_EXPLORE_FINE_COUNT", 0))
    top_k_cfg = max(1, int(engine.config.get("VF_EXPLORE_TOP_K", 1)))

    def walk_rays_from(cv_idx, cf_idx, best_score, best_entry, pass_num, origin_label):
        """One 8-ray pass from (cv_idx, cf_idx). Returns
        (improved, improvement_cell) where improvement_cell is
        (v_mv, f_mhz, pt) of the first strictly-better cell found.
        Returns (False, None) on convergence or engine stop.

        `best_score` is the scalar score (lower = better) from score_cell
        for the current best; `best_entry` is the corresponding surface
        cell (for log-output formatting — never None if best_score is
        not None)."""
        for name, dv, df in RAY_DIRECTIONS:
            if not engine.running:
                return False, None
            # Drain operator-queued remeasurements between ray directions
            # so a queued cell is picked up within one ray's worth of time
            # (~5-40 min) rather than waiting for the whole pass to finish.
            engine._drain_remeasure_queue()
            if not engine.running:
                return False, None
            v_idx, f_idx = cv_idx, cf_idx
            worse_count = 0
            any_new_measurement = False
            while True:
                nv, nf = v_idx + dv, f_idx + df
                if not (0 <= nv < len(v_grid_asc)) or not (0 <= nf < len(f_grid_asc)):
                    break
                v_idx, f_idx = nv, nf
                pt = measure_or_reuse(v_idx, f_idx)
                if measure_or_reuse._was_measured:
                    any_new_measurement = True
                if pt is None:
                    return False, None  # engine stopped
                s = score_of(pt)
                if s is None:
                    # No-data cell: does not count toward trend-stop.
                    continue
                if best_score is None or s < best_score:
                    return True, (v_grid_asc[v_idx], f_grid_asc[f_idx], pt)
                worse_count += 1
                if worse_count >= trend_n:
                    if any_new_measurement:
                        engine.log(
                            f"Phase V pass {pass_num} ({origin_label}) ray {name}: "
                            f"trend confirmed at {v_grid_asc[v_idx]} mV / "
                            f"{f_grid_asc[f_idx]:.1f} MHz "
                            f"(best {format_metric(best_entry)})"
                        )
                    break
        return False, None

    def run_coarse_walk():
        """Run the 8-ray walk from current_best() to convergence, including
        optional top-K-1 cell re-checks. Termination: an 8-ray pass from
        the current best, followed by optional walks from the next
        (VF_COARSE_TOP_K_RAYS - 1) runner-up cells, all find no
        improvement. Returns (cv_mv, cf_mhz, best_entry) on convergence or
        None if engine was stopped."""
        engine.vf_coarse_rays_checked = []
        pass_num = 0
        while engine.running:
            pass_num += 1
            if pass_num > max_passes:
                engine.log(f"Phase V: re-spawn pass cap reached ({max_passes}); exiting walk")
                cv_idx, cf_idx, best_score, best_entry = current_best()
                return v_grid_asc[cv_idx], f_grid_asc[cf_idx], best_entry
            engine._drain_remeasure_queue()
            if not engine.running:
                return None

            cv_idx, cf_idx, best_score, best_entry = current_best()
            center_key = [v_grid_asc[cv_idx], round(float(f_grid_asc[cf_idx]), 3)]
            if center_key not in engine.vf_coarse_rays_checked:
                engine.vf_coarse_rays_checked.append(center_key)
            improved, improvement_cell = walk_rays_from(
                cv_idx, cf_idx, best_score, best_entry, pass_num, "best"
            )

            if not improved and top_k_rays_cfg >= 2:
                # R5: walk rays from the next top_k_rays_cfg - 1 cells to
                # guard against a single outlier score reading at the
                # winner terminating the walk early. Ranked by score_of so
                # profit mode picks top-profit runners-up, efficiency mode
                # picks lowest-J/TH runners-up.
                scorable = [(score_of(e), e) for e in engine.vf_surface if not e.get("fine")]
                scorable = [(s, e) for s, e in scorable if s is not None]
                scorable.sort(key=lambda pair: pair[0])
                candidates = [e for _, e in scorable]
                walked = {(int(k[0]), round(float(k[1]), 3)) for k in engine.vf_coarse_rays_checked}
                extras_run = 0
                for cell in candidates:
                    if extras_run >= top_k_rays_cfg - 1:
                        break
                    ck = (int(cell["voltage_mv"]), round(float(cell["freq_mhz"]), 3))
                    if ck in walked:
                        continue
                    try:
                        ci = v_grid_asc.index(ck[0])
                        cj = f_grid_asc.index(ck[1])
                    except ValueError:
                        continue  # fine / off-grid cell
                    walked.add(ck)
                    engine.vf_coarse_rays_checked.append([ck[0], ck[1]])
                    extras_run += 1
                    engine.log(
                        f"Phase V pass {pass_num}: top-{extras_run + 1} "
                        f"ray re-check from {ck[0]} mV / {ck[1]:.1f} MHz"
                    )
                    improved, improvement_cell = walk_rays_from(
                        ci, cj, best_score, best_entry, pass_num, f"top-{extras_run + 1}"
                    )
                    if improved:
                        break

            if improved:
                v_new, f_new, pt_new = improvement_cell
                engine.log(
                    f"Phase V: new best {format_metric(pt_new)} at "
                    f"{v_new} mV / {f_new:.1f} MHz — re-spawning 8-ray walk"
                )
                engine._save_checkpoint()
                continue  # restart outer walk loop from new best

            # Pass + top-K ray re-check all found no improvement → converged.
            cv_mv = v_grid_asc[cv_idx]
            cf_mhz = f_grid_asc[cf_idx]
            engine.log("")
            engine.log(
                f"Phase V: coarse walk converged on {cv_mv} mV / "
                f"{cf_mhz:.1f} MHz ({format_metric(best_entry)}) after "
                f"{pass_num} pass(es)"
            )
            return cv_mv, cf_mhz, best_entry
        return None

    def run_fine_grid_around(best_entry):
        """R1/R3: build an N×N fine grid strictly inside the coarse cell's
        Voronoi rectangle at (best_entry.V, best_entry.F). Delete any
        existing coarse-surface entry at that exact (V, F) — the fine
        readings replace it. Stamps `kind="fine"` and `coarse_anchor` on
        each fine cell. No-op if the grid collapses below 2 on either
        axis (edge cell or snap dedup)."""
        fine_count = fine_count_cfg
        v_sorted_desc = sorted(v_grid_asc, reverse=True)
        v_step = (v_sorted_desc[0] - v_sorted_desc[-1]) / max(1, len(v_sorted_desc) - 1)
        f_sorted_desc = sorted(f_grid_asc, reverse=True)
        f_step = (f_sorted_desc[0] - f_sorted_desc[-1]) / max(1, len(f_sorted_desc) - 1)
        # R1: in-cell subdivision. offset_i = ((i - (N-1)/2) / N) * step
        # places all N cells strictly inside (-step/2, +step/2) of the
        # coarse winner — no bleed into neighboring coarse cells.
        v_fine_raw = [
            int(
                round(best_entry["voltage_mv"] + ((i - (fine_count - 1) / 2) / fine_count) * v_step)
            )
            for i in range(fine_count)
        ]
        f_fine_raw = [
            best_entry["freq_mhz"] + ((i - (fine_count - 1) / 2) / fine_count) * f_step
            for i in range(fine_count)
        ]
        # Clamp to PSU/firmware bounds; snap freqs to 3.125 MHz grid; dedup.
        v_fine = sorted({max(11877, min(engine.psu_max_mv, v)) for v in v_fine_raw}, reverse=True)
        f_fine = sorted(
            {
                max(
                    engine.config["VF_EXPLORE_F_MIN"],
                    min(engine.config["VF_EXPLORE_F_MAX"], round(f / 3.125) * 3.125),
                )
                for f in f_fine_raw
            },
            reverse=True,
        )
        if len(v_fine) < 2 or len(f_fine) < 2:
            engine.log(
                f"Phase V: fine grid skipped — after snap+dedup only "
                f"{len(v_fine)} V × {len(f_fine)} F distinct points "
                f"(edge cell or grid too narrow for N={fine_count})"
            )
            return

        engine.log("")
        engine.log(
            f"=== Phase V fine grid around ({best_entry['voltage_mv']} mV, "
            f"{best_entry['freq_mhz']:.1f} MHz): "
            f"{len(v_fine)}×{len(f_fine)} exhaustive points ==="
        )

        # R3: throw out the coarse reading at the winner — the N×N fine
        # grid is strictly higher resolution and the coarse entry should
        # not compete for top-K selection or heatmap display once fine
        # data exists.
        winner_key = (int(best_entry["voltage_mv"]), round(float(best_entry["freq_mhz"]), 3))
        engine.vf_surface = [
            e
            for e in engine.vf_surface
            if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) != winner_key
        ]
        engine.vf_planned_grid = [
            p
            for p in engine.vf_planned_grid
            if (int(p["voltage_mv"]), round(float(p["freq_mhz"]), 3)) != winner_key
        ]
        measured.pop(winner_key, None)

        # Fine-grid anchor: remembered for the per-fine-cell
        # `coarse_anchor` stamp and for dashboard/heatmap replay on resume.
        # Persists across checkpoints.
        engine.vf_fine_anchor = {
            "voltage_mv": int(best_entry["voltage_mv"]),
            "freq_mhz": round(float(best_entry["freq_mhz"]), 3),
        }

        # Publish fine cells as planned so the dashboard renders them as
        # pending before measurement.
        planned_keys = {
            (p["voltage_mv"], round(float(p["freq_mhz"]), 3)) for p in engine.vf_planned_grid
        }
        fine_keys_set = {(int(v), round(float(f), 3)) for v in v_fine for f in f_fine}
        anchor_snapshot = dict(engine.vf_fine_anchor)
        for v in v_fine:
            for f in f_fine:
                k = (int(v), round(float(f), 3))
                if k in planned_keys:
                    continue
                engine.vf_planned_grid.append(
                    {
                        "voltage_mv": int(v),
                        "freq_mhz": round(float(f), 3),
                        "fine": True,
                        "coarse_anchor": dict(anchor_snapshot),
                    }
                )
                planned_keys.add(k)
        engine.vf_skipped = [
            e
            for e in engine.vf_skipped
            if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) not in fine_keys_set
        ]
        engine._save_checkpoint()

        for v_mv in v_fine:
            if not engine.running:
                return
            for f_mhz in f_fine:
                if not engine.running:
                    return
                key = (int(v_mv), round(float(f_mhz), 3))
                if key in measured:
                    continue
                result = engine._measure_vf_point(v_mv, f_mhz, fine=True)
                if result is None:
                    return
                # Stamp kind + coarse_anchor so the dashboard can render the
                # fine cell inside its owning coarse cell and the cell-popup
                # before/after lookup can match it back to the chip-tune.
                result["kind"] = "fine"
                result["coarse_anchor"] = dict(anchor_snapshot)
                engine.vf_surface.append(result)
                measured[key] = result
                engine._save_checkpoint()

    # ---- Coarse walk + top-K selection + per-top-K I1 ray walk. Wrapped
    # in a while-loop so any top-K cell whose ray walk finds a strictly
    # better cell re-spawns the whole coarse walk. Terminates only when
    # every top-K cell's 8-ray walk finds no improvement — enforces the
    # I1 invariant that fine grids never start before coarse rays have
    # been walked from the cell being fine-gridded. ----
    while engine.running:
        # ---- Coarse walk: run once to convergence. Idempotent on resume —
        # re-derives from current_best() and re-walks the rays without
        # re-measuring already-measured cells. ----
        if not engine.vf_top_k_voltages:
            converged = run_coarse_walk()
            if converged is None:
                return  # engine stopped

        # ---- Lock top-K coarse cells pre-fine. Every one of these
        # (V, F) cells is guaranteed I1 ray walk + fine grid before any
        # chip-tune runs. Ranked by raw coarse score — NO voltage dedup —
        # so two cells at the same voltage with different F can both
        # become top-K (they'll get distinct Phase 3 chip-tune windows
        # centered on their respective F). Captured from coarse-only
        # entries so fine-grid reads from an earlier top-K iteration
        # don't bias ranking of the remaining coarse candidates. Entries
        # carry `coarse_rays_walked=False, fine_gridded=False` until
        # their ray walk / fine grid completes; on resume, incomplete
        # entries are picked up and finished. ----
        if not engine.vf_top_k_voltages:
            coarse_candidates = [
                e for e in engine.vf_surface if not e.get("fine") and score_of(e) is not None
            ]
            if not coarse_candidates:
                raise MinerCommandError(
                    "Phase V produced no usable coarse measurements — check log."
                )
            coarse_ranked = sorted(coarse_candidates, key=lambda r: score_of(r))
            engine.vf_top_k_voltages = [
                {
                    "voltage_mv": int(e["voltage_mv"]),
                    "seed_f_mhz": float(e["freq_mhz"]),
                    "coarse_jth": float(e["efficiency_jth"]),
                    "coarse_rays_walked": False,
                    "fine_gridded": False,
                    "vf_source": {
                        "kind": "coarse",
                        "voltage_mv": int(e["voltage_mv"]),
                        "freq_mhz": round(float(e["freq_mhz"]), 3),
                        "coarse_jth": float(e["efficiency_jth"]),
                        "hashrate_ths": e.get("hashrate_ths"),
                        "power_w": e.get("power_w"),
                    },
                }
                for e in coarse_ranked[:top_k_cfg]
            ]
            engine.log("")
            engine.log(
                f"=== Phase V: top-{len(engine.vf_top_k_voltages)} coarse "
                f"cells selected for ray walk + fine grid + chip-tune ==="
            )
            for i, tk in enumerate(engine.vf_top_k_voltages, 1):
                engine.log(
                    f"  {i}. {tk['voltage_mv']} mV @ {tk['seed_f_mhz']:.1f} "
                    f"MHz (coarse {tk['coarse_jth']:.2f} J/TH)"
                )
            engine._save_checkpoint()

        # ---- I1 enforcement: walk 8 rays from every top-K cell that
        # hasn't had them yet. If any ray finds a cell that strictly beats
        # current best, clear vf_top_k_voltages and re-spawn the coarse
        # walk — the top-K selection was stale. Short-circuits when the
        # top-K cell IS the current winner (run_coarse_walk already
        # confirmed its rays) to avoid redundant work in the TOP_K=1 case. ----
        respawn = False
        for idx, tk in enumerate(engine.vf_top_k_voltages, 1):
            if not engine.running:
                return
            if tk.get("coarse_rays_walked"):
                continue
            cv_mv = int(tk["voltage_mv"])
            cf_mhz = round(float(tk["seed_f_mhz"]), 3)
            try:
                cv_idx = v_grid_asc.index(cv_mv)
                cf_idx = f_grid_asc.index(cf_mhz)
            except ValueError:
                # Anchor off the coarse grid (shouldn't happen — top-K is
                # picked from coarse-only entries — but defense in depth).
                # Mark walked so we don't infinite-loop.
                tk["coarse_rays_walked"] = True
                engine._save_checkpoint()
                continue
            cb_v_idx, cb_f_idx, best_score, best_entry = current_best()
            if cv_idx == cb_v_idx and cf_idx == cb_f_idx:
                tk["coarse_rays_walked"] = True
                engine._save_checkpoint()
                continue
            engine._drain_remeasure_queue()
            if not engine.running:
                return
            engine.log("")
            engine.log(
                f"Phase V: I1 ray walk from top-{idx} at {cv_mv} mV / "
                f"{cf_mhz:.1f} MHz ({tk['coarse_jth']:.2f} J/TH)"
            )
            got_better, improvement_cell = walk_rays_from(
                cv_idx, cf_idx, best_score, best_entry, 0, f"top-{idx} I1"
            )
            if got_better:
                v_new, f_new, pt_new = improvement_cell
                engine.log(
                    f"Phase V: top-{idx} I1 ray walk found new best "
                    f"{format_metric(pt_new)} at {v_new} mV / "
                    f"{f_new:.1f} MHz — re-spawning coarse walk"
                )
                engine.vf_top_k_voltages = []
                engine._save_checkpoint()
                respawn = True
                break
            tk["coarse_rays_walked"] = True
            engine._save_checkpoint()

        if respawn:
            continue  # re-enter coarse walk → re-select top-K → re-walk I1
        break  # every top-K cell's 8-ray walk confirmed — I1 satisfied

    if not engine.running:
        return

    # ---- Fine-grid each top-K coarse cell sequentially. Skips entries
    # already fine-gridded (resume case). Disabled when FINE_COUNT < 2 —
    # entries are marked fine_gridded=True so the refinement loop
    # proceeds with raw coarse seeds. ----
    if fine_count_cfg < 2:
        for tk in engine.vf_top_k_voltages:
            tk["fine_gridded"] = True
    else:
        for idx, tk in enumerate(engine.vf_top_k_voltages, 1):
            if not engine.running:
                return
            if tk.get("fine_gridded"):
                continue  # already done — skip (handles pre-I1 legacy too)
            # I1 assertion: fine grid must not start without coarse rays
            # walked from this cell. The while-loop above is supposed to
            # have walked them; this guard catches any future refactor
            # that breaks the invariant.
            if not tk.get("coarse_rays_walked"):
                raise MinerCommandError(
                    f"I1 violation: top-{idx} ({tk['voltage_mv']} mV, "
                    f"{tk['seed_f_mhz']:.1f} MHz) reached fine-grid loop "
                    f"without coarse_rays_walked=True"
                )
            cell_proxy = {
                "voltage_mv": int(tk["voltage_mv"]),
                "freq_mhz": round(float(tk["seed_f_mhz"]), 3),
                "efficiency_jth": float(tk["coarse_jth"]),
            }
            engine.log("")
            engine.log(
                f"=== Phase V: fine-gridding top-{idx} cell "
                f"({cell_proxy['voltage_mv']} mV, "
                f"{cell_proxy['freq_mhz']:.1f} MHz) ==="
            )
            run_fine_grid_around(cell_proxy)
            if not engine.running:
                return
            tk["fine_gridded"] = True
            engine._save_checkpoint()

    # ---- Step 3: every unmeasured coarse cell becomes a skip. After the
    # 8-ray walk the final measured set is fixed; remaining cells are
    # off-envelope (the walk never reached them). ----
    measured_keys = set(measured.keys())
    engine.vf_skipped = [
        e
        for e in engine.vf_skipped
        if (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) not in measured_keys
    ]
    skipped_keys = {
        (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)) for e in engine.vf_skipped
    }
    for v_idx in range(len(v_grid_asc)):
        for f_idx in range(len(f_grid_asc)):
            v_mv, f_mhz = grid_point(v_idx, f_idx)
            k = (int(v_mv), round(float(f_mhz), 3))
            if k in measured_keys or k in skipped_keys:
                continue
            engine.vf_skipped.append(
                {
                    "voltage_mv": int(v_mv),
                    "freq_mhz": round(float(f_mhz), 3),
                }
            )
            skipped_keys.add(k)
    engine._save_checkpoint()

    # ---- Refine seed_f_mhz per top-K entry from the post-fine surface.
    # Each top-K is a specific (V, F) coarse cell; its fine grid sits
    # strictly inside that cell's Voronoi rectangle, and every fine entry
    # is stamped with `coarse_anchor: (anchor_v, anchor_f)`. Refinement
    # for top-K[i] picks the best-scoring fine cell whose coarse_anchor
    # matches top-K[i]'s anchor — so two top-K cells at the same voltage
    # but different F don't bleed into each other's refinement. Fallback:
    # keep the original seed_f_mhz if no fine cells exist for this anchor
    # (e.g. VF_EXPLORE_FINE_COUNT < 2 disabled fine grids globally).
    # Stamps `vf_source` to the refined cell so the chip-tune popup can
    # join on the right surface cell for before/after display. ----
    for tk in engine.vf_top_k_voltages:
        anchor_v = int(tk["voltage_mv"])
        anchor_f = round(float(tk["seed_f_mhz"]), 3)
        best_entry = None
        best_score = None
        for e in engine.vf_surface:
            # Only fine cells from THIS top-K's anchor — scope by
            # coarse_anchor stamp so same-voltage top-K siblings stay
            # independent. Coarse-only top-K (VF_EXPLORE_FINE_COUNT < 2)
            # falls through to the fallback below.
            if not e.get("fine"):
                continue
            anc = e.get("coarse_anchor") or {}
            if (
                int(anc.get("voltage_mv", -1)) != anchor_v
                or round(float(anc.get("freq_mhz", -1)), 3) != anchor_f
            ):
                continue
            s = score_of(e)
            if s is None:
                continue
            if best_score is None or s < best_score:
                best_score = s
                best_entry = e
        if best_entry is not None:
            tk["seed_f_mhz"] = float(best_entry["freq_mhz"])
            tk["coarse_jth"] = float(best_entry["efficiency_jth"])
            tk["vf_source"] = {
                "kind": "fine",
                "voltage_mv": int(best_entry["voltage_mv"]),
                "freq_mhz": round(float(best_entry["freq_mhz"]), 3),
                "coarse_jth": float(best_entry["efficiency_jth"]),
                "hashrate_ths": best_entry.get("hashrate_ths"),
                "power_w": best_entry.get("power_w"),
            }
    coarse_measured = sum(1 for e in engine.vf_surface if not e.get("fine"))
    fine_measured = sum(1 for e in engine.vf_surface if e.get("fine"))
    engine.log("")
    engine.log(
        f"=== Phase V complete: {coarse_measured}/{total_points} coarse "
        f"({len(engine.vf_skipped)} off-envelope)"
        f"{f', {fine_measured} fine' if fine_measured else ''}, "
        f"top-{len(engine.vf_top_k_voltages)} voltages for per-chip tuning ==="
    )
    for i, entry in enumerate(engine.vf_top_k_voltages):
        engine.log(
            f"  {i + 1}. {entry['voltage_mv']} mV @ {entry['seed_f_mhz']:.1f} MHz "
            f"({entry['coarse_jth']:.2f} J/TH)"
        )
    engine._save_checkpoint()
