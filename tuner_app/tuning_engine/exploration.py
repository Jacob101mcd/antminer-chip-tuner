"""
Free functions extracted from TuningEngine class for exploration logic.
These functions encapsulate the core exploration algorithms for finding
next measurement points in voltage-frequency tuning space.
"""

from __future__ import annotations

from tuner_app.profit.compute import score_cell


def next_unmeasured_in_direction(
    engine,
    origin_v_idx,
    origin_f_idx,
    dv,
    df,
    v_grid_asc,
    f_grid_asc,
    by_key,
    best_score,
    trend_n,
    score_ctx,
):
    """Walk outward from origin in direction (dv, df). Return (v_mv,
    f_mhz) of the first unmeasured cell along that ray, or None if the
    direction is "done" (grid edge OR trend-confirmed: TREND_CONFIRM
    consecutive measured cells with score > current best).

    Trend-confirm correctness under dynamic best_score: best_score is
    monotonically non-increasing (only strict improvements update it),
    so a cell that was "worse than yesterday's best" is still "worse than
    today's better best" — the trend determination is stable. Cells with
    no score (None J/TH or profit mode + no coin data) are walked past
    without counting toward the trend."""
    v_idx, f_idx = origin_v_idx, origin_f_idx
    worse_count = 0
    # Safety bound — grid is finite, but defend against infinite loops if
    # axes ever get malformed.
    max_steps = len(v_grid_asc) + len(f_grid_asc) + 2
    for _ in range(max_steps):
        nv, nf = v_idx + dv, f_idx + df
        if not (0 <= nv < len(v_grid_asc)) or not (0 <= nf < len(f_grid_asc)):
            return None
        v_idx, f_idx = nv, nf
        v_mv = v_grid_asc[v_idx]
        f_mhz = round(float(f_grid_asc[f_idx]), 3)
        key = (int(v_mv), f_mhz)
        existing = by_key.get(key)
        if existing is None:
            return (v_mv, f_mhz)
        s = score_cell(existing, *score_ctx)
        if s is None:
            # No-data cell: walk past, doesn't count toward trend.
            continue
        if best_score is None or s > best_score:
            worse_count += 1
            if worse_count >= trend_n:
                return None
        else:
            worse_count = 0  # Found a cell at-or-below best — reset
    return None


def find_next_coarse_to_measure(engine):
    """Strict ordering: top-VF_COARSE_TOP_K_RAYS coarse cells (by current
    scoring, with converted-to-fine anchors preserved as ray origins via
    `_top_coarse_ray_origins`), for each one walk 8 directions in
    ridge-first order, return the first unmeasured cell. None when every
    ray from every top-K cell has hit grid edge or trend-stopped.

    Bootstrap: when vf_surface has no scorable coarse cells yet, returns
    the grid center, then proceeds to corners. Ensures Phase 0 → first
    measurement transition works without a separate "seed" branch."""
    v_grid_asc, f_grid_asc = engine._vf_grid_axes()
    if len(v_grid_asc) < 2 or len(f_grid_asc) < 2:
        return None

    score_ctx = engine._get_scoring_context()
    by_key = engine._vf_surface_by_key()
    coarse_ranked = engine._coarse_cells_ranked(score_ctx)

    if not coarse_ranked:
        # Bootstrap: nothing scorable yet. Measure the grid center, then
        # any other unmeasured cell.
        v_center_idx = len(v_grid_asc) // 2
        f_center_idx = len(f_grid_asc) // 2
        v_mv = v_grid_asc[v_center_idx]
        f_mhz = round(float(f_grid_asc[f_center_idx]), 3)
        if (int(v_mv), f_mhz) not in by_key:
            return (v_mv, f_mhz)
        # Center already measured (probably as a no-data cell). Find any
        # other unmeasured cell. Trend-confirm doesn't help us here since
        # we have no scored cells, so just sweep.
        for vv in sorted(v_grid_asc, reverse=True):
            for ff in sorted(f_grid_asc, reverse=True):
                k = (int(vv), round(float(ff), 3))
                if k not in by_key:
                    return (int(vv), round(float(ff), 3))
        return None

    # Pick top-VF_COARSE_TOP_K_RAYS ray origins. Symmetric with
    # `_top_fine_anchors` — converted-to-fine anchors stay in the pool
    # so the conversion event doesn't shift which cells get rays
    # walked. See `_top_coarse_ray_origins` for the rationale.
    top_k_rays = max(1, int(engine.config.get("VF_COARSE_TOP_K_RAYS", 1)))
    top_cells = engine._top_coarse_ray_origins(score_ctx, top_k_rays)

    # Two complementary safeguards make the conversion event a no-op
    # for ray walking:
    #   1. `_top_coarse_ray_origins` (above) keeps converted anchors as
    #      ray origins. Their rays were walked when they were coarse;
    #      `_next_unmeasured_in_direction` will return None for every
    #      direction now, so the function falls through to fine /
    #      chip-tune instead of opening up the next-best coarse cell's
    #      unwalked rays.
    #   2. `best_score` (below) is computed across the FULL surface
    #      (coarse + fine + converted-anchor entries). Using only
    #      `coarse_ranked[0]` would weaken the threshold the moment a
    #      coarse anchor gets converted (its `fine: True` excludes it
    #      from `_coarse_cells_ranked`, so the next-best coarse cell
    #      becomes the reference). That weakening could resume
    #      previously trend-stopped rays. Pulling `best_score` from
    #      every scored entry preserves the original measurement's
    #      threshold across the conversion.
    scored = []
    for e in engine.vf_surface:
        s = score_cell(e, *score_ctx)
        if s is not None:
            scored.append(s)
    best_score = min(scored) if scored else score_cell(coarse_ranked[0], *score_ctx)
    trend_n = max(1, int(engine.config.get("VF_EXPLORE_TREND_CONFIRM", 2)))

    for cell in top_cells:
        cv_mv = int(cell["voltage_mv"])
        cf_mhz = round(float(cell["freq_mhz"]), 3)
        try:
            cv_idx = v_grid_asc.index(cv_mv)
            cf_idx = f_grid_asc.index(cf_mhz)
        except ValueError:
            continue  # Cell not on current coarse grid (config narrowed)
        for _name, dv, df in engine.RAY_DIRECTIONS:
            next_cell = next_unmeasured_in_direction(
                engine,
                cv_idx,
                cf_idx,
                dv,
                df,
                v_grid_asc,
                f_grid_asc,
                by_key,
                best_score,
                trend_n,
                score_ctx,
            )
            if next_cell is not None:
                return next_cell
    return None


def find_next_fine_to_measure(engine):
    """Top-VF_FINE_TOP_K coarse anchors (by current scoring) get N×N
    fine grids. Return (v_mv, f_mhz, anchor_meta) of the first unmeasured
    fine cell, or None when all candidate anchors have complete fine
    grids (or fine is disabled via VF_EXPLORE_FINE_COUNT < 2).

    Strict-sequential rule: this never returns when find_next_coarse_to_
    measure would. The main loop's else-if structure enforces that
    ordering — fine-grid work blocks until coarse is settled.

    Coarse → fine center conversion: when this function first selects a
    coarse anchor for a fine grid, the existing vf_surface entry at
    (anchor_v, anchor_f) is mutated in place to a fine cell — `fine`
    flips True, `kind` flips "fine", `coarse_anchor` is stamped pointing
    to itself. This makes the original coarse measurement BE the anchor
    cell of the new NxN grid (reused, not re-measured), and fixes the
    downstream rendering: the dashboard's fine sub-grid bucket now
    includes the anchor cell with the small fine-cell font, and the
    chip-tune chipTuneByKey match against vf_source.freq_mhz=anchor_f
    lands on a fine entry rather than a coarse one (so the gold winner
    border draws on the right sub-cell)."""
    fine_count = int(engine.config.get("VF_EXPLORE_FINE_COUNT", 0))
    if fine_count < 2:
        return None  # Fine disabled

    v_grid_asc, f_grid_asc = engine._vf_grid_axes()
    if len(v_grid_asc) < 2 or len(f_grid_asc) < 2:
        return None

    score_ctx = engine._get_scoring_context()
    fine_top_k = max(1, int(engine.config.get("VF_FINE_TOP_K", 3)))
    candidates = engine._top_fine_anchors(score_ctx, fine_top_k)
    if not candidates:
        return None

    by_key = engine._vf_surface_by_key()
    v_step = (v_grid_asc[-1] - v_grid_asc[0]) / max(1, len(v_grid_asc) - 1)
    f_step = (f_grid_asc[-1] - f_grid_asc[0]) / max(1, len(f_grid_asc) - 1)
    v_min = float(v_grid_asc[0])
    v_max = float(v_grid_asc[-1])
    f_min = float(engine.config["VF_EXPLORE_F_MIN"])
    f_max = float(engine.config["VF_EXPLORE_F_MAX"])

    for anchor in candidates:
        anchor_v = int(anchor["voltage_mv"])
        anchor_f = round(float(anchor["freq_mhz"]), 3)
        v_fine, f_fine = engine._fine_cell_offsets_for_anchor(
            anchor_v, anchor_f, v_step, f_step, fine_count, v_min, v_max, f_min, f_max
        )
        if not v_fine or not f_fine:
            continue
        # Degenerate grid (anchor pinned at both bounds simultaneously, or
        # bounds collapsed to anchor): fine grid would be the single anchor
        # cell with nothing to measure. Skip — don't fire the misleading
        # "Converted to anchor cell of NxN grid" log line.
        if len(v_fine) <= 1 and len(f_fine) <= 1:
            continue

        # Lazy coarse → fine center conversion. Idempotent: only fires
        # when the anchor's vf_surface entry doesn't yet have `fine: True`.
        anchor_key = (anchor_v, anchor_f)
        anchor_entry = by_key.get(anchor_key)
        if anchor_entry is not None and not anchor_entry.get("fine"):
            anchor_entry["fine"] = True
            anchor_entry["kind"] = "fine"
            anchor_entry["coarse_anchor"] = {
                "voltage_mv": anchor_v,
                "freq_mhz": anchor_f,
            }
            engine.log(
                f"[fine] Converted coarse anchor ({anchor_v} mV, "
                f"{anchor_f:.3f} MHz) to anchor cell of "
                f"{fine_count}x{fine_count} fine grid."
            )
            engine._save_checkpoint()
            # Re-snapshot by_key so the next missing-cell scan sees the
            # mutation and the anchor's own (V, F) is treated as already
            # measured (the anchor cell is the converted entry itself).
            by_key = engine._vf_surface_by_key()

        for v in v_fine:
            for f in f_fine:
                k = (int(v), round(float(f), 3))
                if k in by_key:
                    continue
                anchor_meta = {
                    "voltage_mv": anchor_v,
                    "freq_mhz": anchor_f,
                }
                return (int(v), round(float(f), 3), anchor_meta)
    return None


def chip_tune_already_done_for(engine, cell):
    """Returns True if voltage_results contains an entry that matches this
    cell within FREQ_SEARCH_TOLERANCE_MHZ on freq. Match priority:
    vf_source.freq_mhz > seed_f_mhz > voltage-only fallback."""
    tol = float(engine.config.get("FREQ_SEARCH_TOLERANCE_MHZ", 7))
    cell_v = int(cell["voltage_mv"])
    cell_f = float(cell["freq_mhz"])
    for r in engine.voltage_results:
        if int(r.get("voltage_mv", -1)) != cell_v:
            continue
        r_seed = None
        vfs = r.get("vf_source") or {}
        if vfs.get("freq_mhz") is not None:
            r_seed = float(vfs["freq_mhz"])
        elif r.get("seed_f_mhz") is not None:
            r_seed = float(r["seed_f_mhz"])
        if r_seed is not None:
            if abs(r_seed - cell_f) <= tol:
                return True
            continue
        # No seed info on this entry (legacy): voltage-only match.
        return True
    return False


def find_next_chip_tune_target(engine):
    """Pick chip-tune candidates from inside the top-VF_FINE_TOP_K coarse
    anchors' fine grids (when fine is enabled), or from top coarse cells
    directly (when fine is disabled). Top-VF_EXPLORE_TOP_K cells overall
    by current scoring. Returns the first one without a matching
    voltage_results entry, or None when all are chip-tuned.

    Strict-sequential rule: relies on find_next_fine_to_measure() and
    find_next_coarse_to_measure() returning None first — i.e. all top
    anchors have complete rays + fine grids. The main loop's else-if
    structure enforces this without an explicit gate here."""
    chip_top_k = max(1, int(engine.config.get("VF_EXPLORE_TOP_K", 1)))
    fine_top_k = max(1, int(engine.config.get("VF_FINE_TOP_K", 3)))
    # Defensive clamp: chip-tune candidates come from inside the top-fine_top_k
    # anchors' fine grids. Operator may set EXPLORE_TOP_K > FINE_TOP_K (validator
    # no longer rejects); keep the documented invariant chip_top_k <= fine_top_k.
    chip_top_k = min(chip_top_k, fine_top_k)
    fine_count = int(engine.config.get("VF_EXPLORE_FINE_COUNT", 0))
    score_ctx = engine._get_scoring_context()
    coarse_ranked = engine._coarse_cells_ranked(score_ctx)

    if fine_count < 2:
        # Fine disabled — chip-tune top coarse cells directly.
        if not coarse_ranked:
            return None
        candidates = coarse_ranked[:chip_top_k]
    else:
        # Build pool of fine cells whose coarse_anchor is in the top-fine
        # anchors. Anchor list pulls in-flight grids (whose anchor was
        # converted from coarse to fine) plus new top-K coarse — without
        # in-flight tracking, the converted anchor would drop out of
        # `coarse_ranked` and chip-tune would never fire on its grid.
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

    if not candidates:
        return None

    for cell in candidates:
        if chip_tune_already_done_for(engine, cell):
            continue
        return {
            "voltage_mv": int(cell["voltage_mv"]),
            "freq_mhz": round(float(cell["freq_mhz"]), 3),
            "vf_source": {
                "kind": "fine" if cell.get("fine") else "coarse",
                "voltage_mv": int(cell["voltage_mv"]),
                "freq_mhz": round(float(cell["freq_mhz"]), 3),
                "coarse_jth": float(cell.get("efficiency_jth", 0) or 0),
                "hashrate_ths": cell.get("hashrate_ths"),
                "power_w": cell.get("power_w"),
            },
        }
    return None
