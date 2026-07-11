"""Tests for the Whatsminer branch of `derive_planned_grid_for_dashboard`.

The Whatsminer (`power_limit_freq_search` strategy) planned-grid must be
emitted as wattage × frequency, not voltage × frequency — the engine sweeps
power_limit / target_freq, not voltage. Cell shape mirrors what
`whatsminer_phases._measure_pl_freq_cell` writes to `vf_surface` so the
frontend's measured-vs-planned cell-key match works uniformly.

The other branches (ePIC / Bixbit / LuxOS `voltage_chip_tune`) stay
voltage-keyed — verified by an alternate fixture.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tuner_app.tuning_engine.status import derive_planned_grid_for_dashboard


def _make_whatsminer_engine():
    engine = MagicMock()
    engine.api.tuning_strategy.return_value = "power_limit_freq_search"
    engine.config = {
        "POWER_LIMIT_W": 4000,
        "WHATSMINER_PL_MIN_W": 2000,
        "WHATSMINER_PL_COUNT": 5,
        "WHATSMINER_FREQ_MIN_MHZ": 400,
        "WHATSMINER_FREQ_MAX_MHZ": 575,
        "WHATSMINER_FREQ_COUNT": 5,
    }
    return engine


def _make_epic_engine_min2_grid():
    engine = MagicMock()
    engine.api.tuning_strategy.return_value = "voltage_chip_tune"
    engine._vf_grid_axes.return_value = ([12000, 13000], [400.0, 500.0])
    engine.config = {"VF_EXPLORE_FINE_COUNT": 0}
    return engine


def test_whatsminer_planned_grid_uses_wattage_keys():
    """Cells carry `power_limit_w` populated and `voltage_mv: None` so the
    frontend's `e[yField]` accessor reads the right axis value.
    """
    engine = _make_whatsminer_engine()
    planned = derive_planned_grid_for_dashboard(engine)
    assert len(planned) == 25  # 5 PL × 5 freq
    for cell in planned:
        assert isinstance(cell["power_limit_w"], int)
        assert cell["voltage_mv"] is None
        assert cell["axis_x_kind"] == "power_limit_w"
        assert cell["fine"] is False
        assert "freq_mhz" in cell
        assert "target_freq_mhz" in cell


def test_whatsminer_planned_grid_axis_values_match_config():
    """PL axis spans [WHATSMINER_PL_MIN_W, POWER_LIMIT_W] in PL_COUNT steps;
    freq axis spans [FREQ_MIN, FREQ_MAX] in FREQ_COUNT steps. Mirrors the
    actual coarse-grid sweep in `_run_whatsminer_pass`.
    """
    engine = _make_whatsminer_engine()
    planned = derive_planned_grid_for_dashboard(engine)
    pl_values = sorted({cell["power_limit_w"] for cell in planned})
    freq_values = sorted({cell["freq_mhz"] for cell in planned})
    # PL axis: 5 points from 2000 to 4000 inclusive.
    assert pl_values == [2000, 2500, 3000, 3500, 4000]
    # Freq axis: 5 points from 400 to 575 inclusive.
    assert freq_values[0] == 400.0
    assert freq_values[-1] == 575.0
    assert len(freq_values) == 5


def test_whatsminer_planned_grid_returns_empty_on_collapsed_axis():
    """When PL_COUNT or FREQ_COUNT is 1, the grid collapses to a line and
    the chart can't render meaningfully — return [] so the dashboard hides
    the chart entirely.
    """
    engine = _make_whatsminer_engine()
    engine.config["WHATSMINER_PL_COUNT"] = 1
    assert derive_planned_grid_for_dashboard(engine) == []


def test_whatsminer_planned_grid_handles_missing_config_keys():
    """Missing knobs return [] rather than raising. Test fixtures + partial
    init shouldn't crash the dashboard.
    """
    engine = MagicMock()
    engine.api.tuning_strategy.return_value = "power_limit_freq_search"
    engine.config = {}
    assert derive_planned_grid_for_dashboard(engine) == []


def test_voltage_chip_tune_planned_grid_keeps_voltage_keys():
    """ePIC / Bixbit / LuxOS stay voltage-keyed — verify the Whatsminer
    short-circuit doesn't fire for other strategies.
    """
    engine = _make_epic_engine_min2_grid()
    planned = derive_planned_grid_for_dashboard(engine)
    assert len(planned) == 4  # 2 V × 2 F
    for cell in planned:
        assert isinstance(cell["voltage_mv"], int)
        assert "power_limit_w" not in cell
        assert "axis_x_kind" not in cell
        assert cell["fine"] is False


def test_planned_grid_falls_back_when_tuning_strategy_raises():
    """If `engine.api.tuning_strategy()` raises (e.g. api not initialized in
    a test fixture), the function falls back to the voltage-grid branch.
    Confirms the `try/except` envelope at the top of the function.
    """
    engine = MagicMock()
    engine.api.tuning_strategy.side_effect = AttributeError("not initialized")
    engine._vf_grid_axes.return_value = ([12000, 13000], [400.0, 500.0])
    engine.config = {"VF_EXPLORE_FINE_COUNT": 0}
    planned = derive_planned_grid_for_dashboard(engine)
    assert len(planned) == 4
    assert all("voltage_mv" in cell for cell in planned)
