from __future__ import annotations

import pytest

from tuner_app.tuning_engine.whatsminer_grid import (
    build_freq_axis,
    build_power_limit_axis,
    freq_to_mode_and_percent,
    mode_and_percent_to_freq,
)


class _StubEngine:
    def __init__(self, config):
        self.config = config


def _make_engine(**overrides):
    config = {
        "POWER_LIMIT_W": 3500,
        "WHATSMINER_PL_MIN_W": 1500,
        "WHATSMINER_PL_COUNT": 5,
        "WHATSMINER_FREQ_MAX_MHZ": 700,
        "WHATSMINER_FREQ_MIN_MHZ": 400,
        "WHATSMINER_FREQ_COUNT": 5,
    }
    config.update(overrides)
    return _StubEngine(config)


def _baselines_three_modes(low=400.0, normal=500.0, high=600.0):
    return {
        "low": {"target_freq": low, "freq_avg": low, "power_w": 1500, "supported": True},
        "normal": {"target_freq": normal, "freq_avg": normal, "power_w": 2500, "supported": True},
        "high": {"target_freq": high, "freq_avg": high, "power_w": 3500, "supported": True},
    }


def test_build_power_limit_axis_descending_endpoints_inclusive():
    engine = _make_engine(POWER_LIMIT_W=3500, WHATSMINER_PL_MIN_W=1500, WHATSMINER_PL_COUNT=5)
    result = build_power_limit_axis(engine)
    expected = [3500, 3000, 2500, 2000, 1500]
    assert result == expected
    assert all(isinstance(x, int) for x in result)


def test_build_power_limit_axis_count_2():
    engine = _make_engine(POWER_LIMIT_W=3500, WHATSMINER_PL_MIN_W=1500, WHATSMINER_PL_COUNT=2)
    result = build_power_limit_axis(engine)
    expected = [3500, 1500]
    assert result == expected


def test_build_freq_axis_descending_with_1mhz_snap():
    engine = _make_engine(
        WHATSMINER_FREQ_MAX_MHZ=700, WHATSMINER_FREQ_MIN_MHZ=400, WHATSMINER_FREQ_COUNT=4
    )
    result = build_freq_axis(engine)
    expected = [700.0, 600.0, 500.0, 400.0]
    assert result == expected
    assert all(x == int(x) for x in result)


def test_build_freq_axis_count_3():
    engine = _make_engine(
        WHATSMINER_FREQ_MAX_MHZ=600, WHATSMINER_FREQ_MIN_MHZ=400, WHATSMINER_FREQ_COUNT=3
    )
    result = build_freq_axis(engine)
    expected = [600.0, 500.0, 400.0]
    assert result == expected


def test_freq_to_mode_and_percent_picks_closest_baseline():
    baselines = _baselines_three_modes()
    result = freq_to_mode_and_percent(520.0, baselines, "normal_only")
    assert result[0] == "normal"
    assert result[1] == pytest.approx(4.0)


def test_freq_to_mode_and_percent_clamps_to_pos_100():
    baselines = _baselines_three_modes()
    result = freq_to_mode_and_percent(2000.0, baselines, "normal_only")
    assert result[1] <= 100.0


def test_freq_to_mode_and_percent_clamps_to_neg_100():
    baselines = _baselines_three_modes()
    result = freq_to_mode_and_percent(10.0, baselines, "normal_only")
    assert result[1] >= -100.0


def test_freq_to_mode_and_percent_skips_unsupported_modes():
    baselines = _baselines_three_modes()
    baselines["high"]["supported"] = False
    result = freq_to_mode_and_percent(600.0, baselines, "normal_only")
    assert result[0] != "high"


def test_freq_to_mode_and_percent_falls_back_when_clamped():
    baselines = _baselines_three_modes()
    result = freq_to_mode_and_percent(10.0, baselines, "normal_only")
    assert result[0] == "low" or result[0] == "normal"


def test_mode_and_percent_to_freq_current_mode():
    baselines = _baselines_three_modes()
    result = mode_and_percent_to_freq("normal", 10.0, baselines, "current_mode")
    assert result == 550.0


def test_mode_and_percent_to_freq_normal_only():
    baselines = _baselines_three_modes()
    result = mode_and_percent_to_freq("low", 10.0, baselines, "normal_only")
    assert result == 550.0


def test_round_trip_freq_mode_percent():
    baselines = _baselines_three_modes()
    test_freqs = [400.0, 500.0, 600.0]
    for freq in test_freqs:
        mode, percent = freq_to_mode_and_percent(freq, baselines, "normal_only")
        result = mode_and_percent_to_freq(mode, percent, baselines, "normal_only")
        assert abs(result - freq) <= 1.0


def test_anchor_difference_produces_different_results():
    baselines = _baselines_three_modes()
    mode, percent = "low", 10.0
    result1 = mode_and_percent_to_freq(mode, percent, baselines, "current_mode")
    result2 = mode_and_percent_to_freq(mode, percent, baselines, "normal_only")
    assert result1 != result2


def test_freq_to_mode_and_percent_empty_baselines_raises():
    with pytest.raises((KeyError, ValueError)):
        freq_to_mode_and_percent(500.0, {}, "normal_only")
