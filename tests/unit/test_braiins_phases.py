"""Unit tests for tuner_app.tuning_engine.braiins_phases helpers.

Tests cover the pure-algorithm helpers in isolation using a minimal stub
engine.  `run_braiins_loop` end-to-end is intentionally NOT tested here
(too many side effects — relegated to a future integration test).
"""

import math
import unittest
from unittest.mock import MagicMock

from tuner_app.tuning_engine.braiins_phases import (
    _compute_sample_profit,
    _init_search_bounds,
    _narrow_bounds,
    _recent_sample,
    _select_best,
)

# ---------------------------------------------------------------------------
# Minimal stub engine
# ---------------------------------------------------------------------------


class _StubEngine:
    """Minimal engine stand-in for braiins_phases helpers.

    Provides only the attributes the algorithm reads / writes, with sane
    defaults.  Tests that need different values set them before calling the
    helper under test.
    """

    def __init__(self):
        self.wattage_results: list = []
        self.wattage_search_low = None
        self.wattage_search_high = None
        self.best_wattage_w = None
        self.running = True
        self.api = MagicMock()
        self.api.firmware_type.return_value = "braiins"
        self._save_profile = MagicMock()
        self._save_checkpoint = MagicMock()
        self.log = MagicMock()
        # Config dict with sensible defaults matching CONFIG_DEFAULTS
        self._config: dict = {
            "BRAIINS_POWER_MIN_W": 1500,
            "BRAIINS_POWER_MAX_W": 5000,
            "BRAIINS_TUNER_STABILIZE_WAIT_SEC": 600,
            "BRAIINS_BINARY_SEARCH_TOLERANCE_W": 100,
            "PERPETUAL_VOLTAGE_CHECK_MIN": 10,
        }

        class _FakeConfig:
            def __init__(self, d):
                self._d = d

            def get(self, key, default=None):
                return self._d.get(key, default)

            def __getitem__(self, key):
                return self._d[key]

        self.config = _FakeConfig(self._config)

    def _set_config(self, key, value):
        self._config[key] = value


def _sample(
    watt: int,
    profit: float | None = None,
    eff: float | None = None,
    ts: float = 0.0,
) -> dict:
    """Build a minimal wattage_results entry."""
    hashrate_ths = 100.0  # non-zero so _compute_sample_profit doesn't early-return
    return {
        "watt": watt,
        "hashrate_ths": hashrate_ths,
        "power_w_actual": float(watt),
        "efficiency_jth": eff,
        "profit_usd_per_day": profit,
        "fan_speed": 3600,
        "ts": ts,
    }


# ===========================================================================
# TestNarrowBounds
# ===========================================================================


class TestNarrowBounds(unittest.TestCase):
    """_narrow_bounds() three-way comparison rule."""

    def test_narrow_brackets_when_mid_best(self):
        """When mid profit >= both low and high, window narrows around mid."""
        engine = _StubEngine()
        low_w, high_w = 1000, 5000
        engine.wattage_search_low = low_w
        engine.wattage_search_high = high_w
        mid_w = (low_w + high_w) // 2  # 3000

        low_s = _sample(low_w, profit=1.0)
        mid_s = _sample(mid_w, profit=2.0)
        high_s = _sample(high_w, profit=0.5)
        engine.wattage_results = [low_s, mid_s, high_s]

        _narrow_bounds(engine, low_s, mid_s, high_s)

        # delta = max((5000-1000)//4, 1) = 1000
        expected_low = max(low_w, mid_w - 1000)
        expected_high = min(high_w, mid_w + 1000)
        self.assertEqual(engine.wattage_search_low, expected_low)
        self.assertEqual(engine.wattage_search_high, expected_high)
        # New window is strictly narrower than original
        self.assertGreater(engine.wattage_search_low, low_w)
        self.assertLess(engine.wattage_search_high, high_w)

    def test_narrow_high_when_high_best(self):
        """When high profit > low profit and mid < high, low moves up to mid_w."""
        engine = _StubEngine()
        low_w, high_w = 1000, 5000
        engine.wattage_search_low = low_w
        engine.wattage_search_high = high_w
        mid_w = (low_w + high_w) // 2

        # p_high(3.0) > p_low(0.5), and p_mid(1.0) < p_high(3.0) → high-best
        low_s = _sample(low_w, profit=0.5)
        mid_s = _sample(mid_w, profit=1.0)
        high_s = _sample(high_w, profit=3.0)
        engine.wattage_results = [low_s, mid_s, high_s]

        _narrow_bounds(engine, low_s, mid_s, high_s)
        self.assertEqual(engine.wattage_search_low, mid_w)
        self.assertEqual(engine.wattage_search_high, high_w)

    def test_narrow_low_when_low_best(self):
        """When low profit >= mid and >= high, high bound moves to mid_w."""
        engine = _StubEngine()
        low_w, high_w = 1000, 5000
        engine.wattage_search_low = low_w
        engine.wattage_search_high = high_w
        mid_w = (low_w + high_w) // 2

        low_s = _sample(low_w, profit=3.0)
        mid_s = _sample(mid_w, profit=1.0)
        high_s = _sample(high_w, profit=0.5)
        engine.wattage_results = [low_s, mid_s, high_s]

        _narrow_bounds(engine, low_s, mid_s, high_s)
        self.assertEqual(engine.wattage_search_low, low_w)
        self.assertEqual(engine.wattage_search_high, mid_w)

    def test_narrow_handles_minimum_delta(self):
        """With (high-low)==4, delta=1 — still produces a non-degenerate range."""
        engine = _StubEngine()
        low_w, high_w = 1000, 1004
        engine.wattage_search_low = low_w
        engine.wattage_search_high = high_w
        mid_w = (low_w + high_w) // 2  # 1002

        low_s = _sample(low_w, profit=1.0)
        mid_s = _sample(mid_w, profit=2.0)  # mid is best → narrow around mid
        high_s = _sample(high_w, profit=0.5)
        engine.wattage_results = [low_s, mid_s, high_s]

        _narrow_bounds(engine, low_s, mid_s, high_s)
        # delta = max((1004-1000)//4, 1) = max(1, 1) = 1
        # new_low = max(1000, 1002-1) = 1001
        # new_high = min(1004, 1002+1) = 1003
        self.assertGreaterEqual(engine.wattage_search_low, low_w)
        self.assertLessEqual(engine.wattage_search_high, high_w)
        # Range is still non-degenerate
        self.assertGreater(engine.wattage_search_high, engine.wattage_search_low)


# ===========================================================================
# TestSelectBest
# ===========================================================================


class TestSelectBest(unittest.TestCase):
    """_select_best() picks the highest-ranking wattage_results entry."""

    def test_select_best_picks_highest_profit_when_minerstat_present(self):
        engine = _StubEngine()
        engine.wattage_results = [
            _sample(2000, profit=1.0),
            _sample(3000, profit=3.5),
            _sample(4000, profit=2.0),
        ]
        self.assertEqual(_select_best(engine), 3000)

    def test_select_best_falls_back_to_efficiency_when_no_profit(self):
        """With no profit data, lowest J/TH wins (negated eff → higher rank)."""
        engine = _StubEngine()
        engine.wattage_results = [
            _sample(2000, profit=None, eff=20.0),  # rank: -20.0
            _sample(3000, profit=None, eff=18.0),  # rank: -18.0  ← best
            _sample(4000, profit=None, eff=22.0),  # rank: -22.0
        ]
        self.assertEqual(_select_best(engine), 3000)

    def test_select_best_returns_none_when_empty(self):
        engine = _StubEngine()
        engine.wattage_results = []
        self.assertIsNone(_select_best(engine))


# ===========================================================================
# TestComputeSampleProfit
# ===========================================================================


class TestComputeSampleProfit(unittest.TestCase):
    """_compute_sample_profit() scalar ranking rules."""

    def _engine(self):
        return _StubEngine()

    def test_returns_profit_when_present(self):
        engine = self._engine()
        s = _sample(3000, profit=2.5)
        self.assertAlmostEqual(_compute_sample_profit(engine, s), 2.5)

    def test_returns_neg_inf_on_none_sample(self):
        engine = self._engine()
        self.assertEqual(_compute_sample_profit(engine, None), -math.inf)

    def test_returns_neg_inf_on_zero_hashrate(self):
        engine = self._engine()
        s = _sample(3000, profit=1.0)
        s["hashrate_ths"] = 0.0
        self.assertEqual(_compute_sample_profit(engine, s), -math.inf)

    def test_returns_neg_eff_when_no_profit(self):
        engine = self._engine()
        s = _sample(3000, profit=None, eff=18.0)
        self.assertAlmostEqual(_compute_sample_profit(engine, s), -18.0)

    def test_returns_neg_inf_when_eff_also_none(self):
        engine = self._engine()
        s = _sample(3000, profit=None, eff=None)
        self.assertEqual(_compute_sample_profit(engine, s), -math.inf)


# ===========================================================================
# TestRecentSample
# ===========================================================================


class TestRecentSample(unittest.TestCase):
    """_recent_sample() toleranced lookup into wattage_results."""

    def test_recent_sample_finds_within_tolerance(self):
        engine = _StubEngine()
        s = _sample(3000, ts=1000.0)
        engine.wattage_results = [s]
        result = _recent_sample(engine, 3050, tolerance_w=100)
        self.assertIs(result, s)

    def test_recent_sample_outside_tolerance_returns_none(self):
        engine = _StubEngine()
        engine.wattage_results = [_sample(3000, ts=1000.0)]
        result = _recent_sample(engine, 3500, tolerance_w=100)
        self.assertIsNone(result)

    def test_recent_sample_exact_match(self):
        engine = _StubEngine()
        s = _sample(3000, ts=500.0)
        engine.wattage_results = [s]
        result = _recent_sample(engine, 3000, tolerance_w=0)
        self.assertIs(result, s)

    def test_recent_sample_returns_most_recent_when_multiple_match(self):
        engine = _StubEngine()
        s_old = _sample(3000, ts=100.0)
        s_new = _sample(3000, ts=999.0)
        engine.wattage_results = [s_old, s_new]
        result = _recent_sample(engine, 3000, tolerance_w=50)
        self.assertIs(result, s_new)

    def test_recent_sample_empty_results_returns_none(self):
        engine = _StubEngine()
        result = _recent_sample(engine, 3000, tolerance_w=100)
        self.assertIsNone(result)


# ===========================================================================
# TestInitSearchBounds
# ===========================================================================


class TestInitSearchBounds(unittest.TestCase):
    """_init_search_bounds() clamp and set from CONFIG."""

    def test_init_search_bounds_uses_config_when_unset(self):
        """With custom CONFIG values, engine gets those as bounds."""
        engine = _StubEngine()
        engine._set_config("BRAIINS_POWER_MIN_W", 2000)
        engine._set_config("BRAIINS_POWER_MAX_W", 4500)
        _init_search_bounds(engine)
        self.assertEqual(engine.wattage_search_low, 2000)
        self.assertEqual(engine.wattage_search_high, 4500)

    def test_init_search_bounds_uses_default_config_values(self):
        """With default CONFIG values, bounds are 1500..5000."""
        engine = _StubEngine()
        _init_search_bounds(engine)
        self.assertEqual(engine.wattage_search_low, 1500)
        self.assertEqual(engine.wattage_search_high, 5000)

    def test_init_search_bounds_handles_inverted(self):
        """If high <= low, _init_search_bounds produces high = low+1 (non-degenerate)."""
        engine = _StubEngine()
        engine._set_config("BRAIINS_POWER_MIN_W", 3000)
        engine._set_config("BRAIINS_POWER_MAX_W", 3000)  # equal → guard fires
        _init_search_bounds(engine)
        self.assertGreater(engine.wattage_search_high, engine.wattage_search_low)

    def test_init_search_bounds_strictly_inverted(self):
        """If configured high < low, _init_search_bounds produces high = low+1."""
        engine = _StubEngine()
        engine._set_config("BRAIINS_POWER_MIN_W", 4000)
        engine._set_config("BRAIINS_POWER_MAX_W", 2000)  # explicitly inverted
        _init_search_bounds(engine)
        self.assertGreater(engine.wattage_search_high, engine.wattage_search_low)
