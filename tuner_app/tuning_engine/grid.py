"""Phase V grid construction, ranking, and ray-walk helpers.

Voronoi half-extent ((n-1)/(2n) × step) keeps fine sub-cells strictly
inside their parent coarse rectangle so the dashboard heatmap doesn't
bleed across cell boundaries.
"""

from __future__ import annotations

from tuner_app.constants import FIRMWARE_FREQ_MIN_MHZ
from tuner_app.profit.compute import score_cell


def vf_grid_axes(engine):
    """Build (v_grid_asc, f_grid_asc) from current config. Recomputed
    every iteration so config changes (e.g. operator widening F_MAX)
    take effect on the next find_next_* call."""
    v_grid = build_vf_grid_voltages(engine)
    f_grid = build_vf_grid_freqs(engine)
    v_grid_asc = sorted(set(int(v) for v in v_grid))
    f_grid_asc = sorted({round(float(f), 3) for f in f_grid})
    return v_grid_asc, f_grid_asc


def vf_surface_by_key(engine):
    """{(v_int, f_round3): entry} for fast O(1) measured-cell lookup."""
    return {(int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3)): e for e in engine.vf_surface}


def coarse_cells_ranked(engine, score_ctx=None):
    """Return coarse vf_surface entries sorted best-first by current
    scoring (J/TH ascending, or $/day descending in profit mode). Excludes
    fine cells and cells with no score (None J/TH or profit mode + no
    coin data)."""
    if score_ctx is None:
        score_ctx = engine._get_scoring_context()
    cells = []
    for e in engine.vf_surface:
        if e.get("fine"):
            continue
        s = score_cell(e, *score_ctx)
        if s is None:
            continue
        cells.append((s, e))
    cells.sort(key=lambda p: p[0])
    return [e for _, e in cells]


def top_coarse_ray_origins(engine, score_ctx, top_k_rays):
    """Return up to top_k_rays vf_surface entries that should currently be
    treated as ray-walk origins for coarse exploration. Symmetric to
    `_top_fine_anchors`: combines in-flight converted anchors (entries
    with `fine: True` whose `coarse_anchor` self-points — i.e. they were
    the coarse top-K cell at conversion time) with the current top-K
    coarse cells, in-flight first.

    Why include converted anchors: their rays were walked while they
    were still coarse cells (when VF_FINE_TOP_K <= VF_COARSE_TOP_K_RAYS
    every converted anchor was in the coarse ray-origin pool at
    conversion time; the operator can now decouple these but the
    typical-case logic still applies). Treating them as ray-origins again returns
    None for every direction (all in-grid unmeasured cells along their
    rays already came back from prior iterations), which is what we
    want — it lets `_find_next_coarse_to_measure` report "no coarse
    work" so the loop falls through to fine / chip-tune. Without this,
    the converted anchor drops out of `_coarse_cells_ranked`, the
    next-best coarse cell (previously beyond top_k_rays) becomes a new
    ray origin, and its unwalked rays surface fresh coarse cells
    mid-fine-phase.

    Score-equivalence: a converted anchor's voltage_mv, freq_mhz, and
    measurement payload are unmutated by conversion, so its score is
    identical to before. Ranking remains stable across the conversion
    event."""
    in_flight = []
    in_flight_keys = set()
    for e in engine.vf_surface:
        if not e.get("fine"):
            continue
        ca = e.get("coarse_anchor") or {}
        try:
            ck = (int(ca["voltage_mv"]), round(float(ca["freq_mhz"]), 3))
        except (KeyError, TypeError, ValueError):
            continue
        # Self-pointing filter: only the converted anchor cell itself
        # is a ray origin. Non-anchor fine cells in the same grid have
        # `coarse_anchor` pointing at a different (V, F) — that other
        # cell is the actual converted anchor entry; this cell is one
        # of its grid neighbors and should not be treated as an origin.
        e_key = (int(e["voltage_mv"]), round(float(e["freq_mhz"]), 3))
        if ck != e_key:
            continue
        if e_key in in_flight_keys:
            continue
        if score_cell(e, *score_ctx) is None:
            continue  # thermal_failed or no efficiency_jth
        in_flight_keys.add(e_key)
        in_flight.append(e)
    coarse_ranked = coarse_cells_ranked(engine, score_ctx)
    new_origins = [
        c
        for c in coarse_ranked
        if (int(c["voltage_mv"]), round(float(c["freq_mhz"]), 3)) not in in_flight_keys
    ]
    n_remaining = max(0, top_k_rays - len(in_flight))
    return in_flight[:top_k_rays] + new_origins[:n_remaining]


def fine_axis_offsets(engine, anchor, step, lo_bound, hi_bound, n):
    """Place n cell positions including the anchor along one axis.

    The anchor is always one of the returned values. Cells stay strictly
    inside the anchor's Voronoi half-extent ((n-1)/(2n) × step) so the
    sub-grid never bleeds into a neighboring coarse cell visually, AND
    stay inside [lo_bound, hi_bound] so corner/edge anchors don't probe
    past the operator's chosen exploration region.

    Layout:
      - Interior anchor (full Voronoi room on both sides) → anchor at the
        geometric center, spacing = step/n. Identical to the old
        strictly-inside formula.
      - Edge anchor (one bound clamps the available room on one side) →
        grid shifts/compresses toward the constrained side. The anchor's
        position within the n cells moves toward that edge; spacing
        shrinks. All n cells fit inside both constraints.
      - Anchor pinned at both bounds simultaneously (degenerate config)
        → returns [anchor] × 1, the caller will dedup-collapse to a
        single cell.

    This keeps every fine cell within the coarse cell represented by its anchor."""
    if n <= 1:
        return [anchor]
    # Voronoi half-extent — strictly less than ±step/2 so cells don't
    # touch the neighboring coarse cell's pixel rectangle.
    max_off = (n - 1) / (2 * n) * step
    below = min(max_off, anchor - lo_bound)
    above = min(max_off, hi_bound - anchor)
    if below <= 0 and above <= 0:
        return [anchor]
    n_total = n - 1  # other-than-anchor cells
    frac_below = below / (below + above) if (below + above) > 0 else 0.5
    n_below = max(0, min(n_total, round(n_total * frac_below)))
    n_above = n_total - n_below
    # Use the smaller of the two per-side spacings as the uniform spacing.
    # This keeps both sides inside their available extent — the under-used
    # side just doesn't reach all the way to its edge, which is fine.
    constraints = []
    if n_below > 0:
        constraints.append(below / n_below)
    if n_above > 0:
        constraints.append(above / n_above)
    spacing = min(constraints) if constraints else 0
    return [anchor + (i - n_below) * spacing for i in range(n)]


def fine_cell_offsets_for_anchor(
    engine, anchor_v, anchor_f, v_step, f_step, fine_count, v_min, v_max, f_min, f_max
):
    """Compute the N×N fine-cell positions for an anchor.

    See `_fine_axis_offsets` for the per-axis layout. Cells are clamped
    to absolute hardware safety bounds (PSU min/max for voltage,
    FIRMWARE_FREQ_MIN_MHZ floor + 3.125 MHz quantization for freq) and
    deduped (the firmware-rounded freq can collapse adjacent cells when
    spacing is < 3.125 MHz, e.g. at N=49 with default coarse F step).

    Returns (v_fine, f_fine) lists sorted descending. Worst case is
    ([anchor_v], [anchor_f]) when both axes are degenerate; the caller
    treats that as a single-cell grid which is naturally satisfied by
    the anchor's existing measurement (no new cells to enumerate)."""
    psu_floor = max(11877, int(v_min))
    psu_ceiling = int(min(engine.psu_max_mv, v_max))
    fw_freq_floor = max(FIRMWARE_FREQ_MIN_MHZ, float(f_min))
    fw_freq_ceiling = float(f_max)
    v_raw = fine_axis_offsets(engine, int(anchor_v), v_step, psu_floor, psu_ceiling, fine_count)
    f_raw = fine_axis_offsets(
        engine, float(anchor_f), f_step, fw_freq_floor, fw_freq_ceiling, fine_count
    )
    v_fine = sorted({int(round(v)) for v in v_raw}, reverse=True)
    f_fine = sorted({round(round(f / 3.125) * 3.125, 3) for f in f_raw}, reverse=True)
    return v_fine, f_fine


def top_fine_anchors(engine, score_ctx, fine_top_k):
    """Return the list of coarse-anchor descriptors that should currently
    have fine grids — used by `_find_next_fine_to_measure`,
    `_find_next_chip_tune_target`, and `_derive_top_k_for_dashboard` so
    all three agree on which anchors are "fine candidates".

    Combines two pools:
    1. In-flight: unique `coarse_anchor` values among existing fine
       entries in vf_surface. Anchors that already had their coarse →
       fine conversion fall here (the converted entry has `fine: True`,
       which makes _coarse_cells_ranked exclude it from the coarse pool).
       Without tracking these explicitly, the rest of an in-flight
       grid's cells would never get measured.
    2. New: top-VF_FINE_TOP_K coarse cells not yet in-flight.

    In-flight gets priority slots. If a settings change shrinks
    fine_top_k below len(in_flight), extra in-flight grids are abandoned
    for now (they sit incomplete in vf_surface). Each entry in the
    returned list is `{voltage_mv: int, freq_mhz: float (round 3)}`."""
    in_flight = {}
    for e in engine.vf_surface:
        if not e.get("fine"):
            continue
        ca = e.get("coarse_anchor")
        if not ca:
            continue
        try:
            ck = (int(ca["voltage_mv"]), round(float(ca["freq_mhz"]), 3))
        except (KeyError, TypeError, ValueError):
            continue
        in_flight.setdefault(
            ck,
            {
                "voltage_mv": int(ca["voltage_mv"]),
                "freq_mhz": round(float(ca["freq_mhz"]), 3),
            },
        )
    coarse_ranked = coarse_cells_ranked(engine, score_ctx)
    new_candidates = []
    for c in coarse_ranked:
        ck = (int(c["voltage_mv"]), round(float(c["freq_mhz"]), 3))
        if ck in in_flight:
            continue
        new_candidates.append(
            {
                "voltage_mv": int(c["voltage_mv"]),
                "freq_mhz": round(float(c["freq_mhz"]), 3),
            }
        )
    in_flight_list = list(in_flight.values())
    n_remaining = max(0, fine_top_k - len(in_flight_list))
    return in_flight_list[:fine_top_k] + new_candidates[:n_remaining]


def build_vf_grid_voltages(engine):
    """Return the ordered list of coarse-grid voltages (high → low).

    Top-V = stock + SWEEP_OVER_STOCK_MV (clamped to psu_max); falls back
    to psu_max - 500 when no stock baseline is available. Bottom-V =
    START_VOLTAGE_MV, which Phase 0 populates from /capabilities' PSU
    min when the operator leaves it at 0."""
    v_count = max(2, int(engine.config["VF_EXPLORE_V_COUNT"]))
    over_stock = int(engine.config["SWEEP_OVER_STOCK_MV"])
    stock_v = 0
    if engine.stock_baseline and engine.stock_baseline.get("source") in ("live", "spec"):
        stock_v = int(engine.stock_baseline.get("voltage_mv", 0) or 0)
    if stock_v > 0:
        v_max = min(stock_v + over_stock, engine.psu_max_mv)
    else:
        v_max = max(engine.psu_max_mv - 500, engine.start_voltage_mv)
    if engine.start_voltage_mv > 0:  # noqa: SIM108
        v_min = engine.start_voltage_mv
    else:
        # Defense-in-depth floor when start_voltage_mv is unset (engine still
        # in pre-Phase-0 state — get_status calls this from HTTP polls).
        # Above the S21's Type-193 PSU minimum (11877 mV). Log emission lives
        # in phase0_discovery so that grid building stays a pure read.
        v_min = 12000
    if v_min >= v_max:
        v_min = max(11877, v_max - 500)
    if v_count <= 1:
        return [int(v_max)]
    step = (v_max - v_min) / (v_count - 1)
    return [int(round(v_max - i * step)) for i in range(v_count)]


def build_vf_grid_freqs(engine, f_min=None, f_max=None, count=None):
    """Return a list of grid frequencies (high → low), snapped to the 3.125
    MHz firmware grid so applying them doesn't silently alias."""
    f_min = engine.config["VF_EXPLORE_F_MIN"] if f_min is None else f_min
    f_max = engine.config["VF_EXPLORE_F_MAX"] if f_max is None else f_max
    count = max(2, int(engine.config["VF_EXPLORE_F_COUNT"] if count is None else count))
    grid = 3.125
    step = (f_max - f_min) / (count - 1)
    out = []
    seen = set()
    for i in range(count):
        raw = f_max - i * step
        snapped = round(raw / grid) * grid
        if snapped not in seen:
            seen.add(snapped)
            out.append(snapped)
    return out
