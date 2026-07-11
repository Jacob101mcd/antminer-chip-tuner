"""Tests that get_status payload's capabilities dict includes the two new
strategy-derived flags: voltage_chip_tune_strategy and power_limit_freq_search_strategy."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuner_app.tuning_engine.status import get_status

_STRATEGY_BY_FIRMWARE = {
    "epic": "voltage_chip_tune",
    "bixbit": "voltage_chip_tune",
    "luxos": "voltage_chip_tune",
    "braiins": "wattage_search",
    "whatsminer": "power_limit_freq_search",
}


class _FakeConfig:
    """Minimal EffectiveConfig-like object."""

    def __init__(self, firmware_type="epic"):
        self._data = {
            "firmware_type": firmware_type,
            "FREQ_SEARCH_TOLERANCE_MHZ": 7,
            "CHIP_FREQ_SPREAD_MHZ": 40,
            "VF_EXPLORE_TREND_CONFIRM": 2,
            "VF_EXPLORE_WAIT": 90,
            "VF_EXPLORE_SAMPLES": 3,
            "VF_EXPLORE_SAMPLE_INTERVAL": 5,
            "VF_EXPLORE_V_COUNT": 5,
            "VF_EXPLORE_F_COUNT": 5,
            "VF_EXPLORE_TOP_K": 1,
            "VF_FINE_TOP_K": 3,
            "VF_COARSE_TOP_K_RAYS": 3,
            "CHIP_TUNE_STEP_MHZ": 6.25,
            "CHIP_TUNE_UP_TOLERANCE": 5,
            "CHIP_TUNE_DOWN_TOLERANCE": 15,
            "CHIP_TUNE_STILLNESS_STREAK": 2,
            "MAX_PROFILING_ROUNDS": 60,
            "STABILITY_POLISH_ROUNDS": 3,
            "STABILITY_POLISH_STEP_MHZ": 6.25,
            "STABILITY_POLISH_ROUND_SAMPLES": 40,
            "STABILITY_POLISH_ROUND_INTERVAL": 30,
            "TARGET_MODE": "efficiency",
            "ELECTRIC_RATE_PER_KWH": 0.10,
            "MINERSTAT_COIN": "BTC",
            "INCOME_MODIFIER_PCT": 0.0,
            "MRR_ENABLED": False,
            "MRR_RIG_ID": 0,
            "MRR_HASHRATE_MODIFIER_PCT": 0.0,
            "MRR_HASHRATE_UNIT": "th",
        }

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data


def _make_mock_engine(firmware_type="epic"):
    """Build a minimal mock engine that satisfies get_status."""
    engine = MagicMock()
    engine.config = _FakeConfig(firmware_type)
    engine.ip = "192.0.2.5"
    engine.mac = "aa:bb:cc:dd:ee:01"
    engine.firmware_type = firmware_type
    engine.phase = "idle"
    engine.phase_detail = ""
    engine.profiling_round = 0
    engine.profiling_completion_pct = 0.0
    engine.chips_stable_pct = 0.0
    engine.chips_converged = 0
    engine.chips_alive = 0
    engine.stillness_streak = 0
    engine.polish_round = 0
    engine.polish_active = False
    engine.min_voltage_mv = 0
    engine.current_step_started_at = None
    engine.tuning_complete = False
    engine.stock_baseline = {}
    engine.best_efficiency = None
    engine.stable_freq_arrays = []
    engine.baseline_scores = []
    engine.baseline_chip_temps = []
    engine.baseline_chip_hashrates = []
    engine.baseline_freq_arrays = []
    engine.voltage_results = []
    engine.vf_surface = {}
    engine.in_flight_chip_tune_target = None
    engine.remeasure_queue = []
    engine.current_vf_point = None
    engine.config_snapshot = {}
    engine.num_boards = 3
    engine.chips_per_board = 108
    engine.active_sweep_voltage_mv = None
    engine.sweep_voltage_mv = None
    engine.sweep_hashrate_ths = None
    engine.voltage_adjustment_mv = 0
    engine.last_restart_ts = None
    engine.offline_since_ts = None
    engine.offline_failure_count = 0
    engine.last_successful_contact_ts = None
    engine.pre_offline_phase = None
    engine.thread = None
    engine.mrr_last_sync = None
    engine.last_summary = None
    engine._derive_top_k_for_dashboard.return_value = ([], None)
    engine._compute_avg_temps_c.return_value = (None, None)
    engine._compute_top_tunes.return_value = []
    engine._derive_planned_grid_for_dashboard.return_value = {}
    engine.api = MagicMock()
    engine.api.tuning_strategy.return_value = _STRATEGY_BY_FIRMWARE[firmware_type]
    engine.api.supports_per_chip_tuning.return_value = True
    engine.api.has_external_power_limit.return_value = True
    engine.api.has_capabilities_endpoint.return_value = True
    engine.api.has_internal_perpetual_tune.return_value = True
    return engine


FIRMWARES = ("epic", "bixbit", "luxos", "braiins", "whatsminer")


class TestStatusCapabilities(unittest.TestCase):
    def test_normal_status_does_not_include_config_snapshot(self):
        engine = _make_mock_engine("epic")
        engine.config_snapshot = {"PASSWORD": "must-not-leak", "SAFE": 1}
        self.assertNotIn("config_snapshot", get_status(engine))

    def test_capabilities_dict_contains_seven_keys_for_each_firmware(self):
        expected_keys = {
            "supports_per_chip_tuning",
            "has_external_power_limit",
            "has_capabilities_endpoint",
            "has_internal_perpetual_tune",
            "wattage_search_strategy",
            "voltage_chip_tune_strategy",
            "power_limit_freq_search_strategy",
        }
        for fw in FIRMWARES:
            with self.subTest(firmware=fw):
                engine = _make_mock_engine(fw)
                result = get_status(engine)
                self.assertIn("capabilities", result)
                self.assertEqual(set(result["capabilities"].keys()), expected_keys)

    def test_voltage_chip_tune_strategy_true_for_epic(self):
        engine = _make_mock_engine("epic")
        self.assertTrue(get_status(engine)["capabilities"]["voltage_chip_tune_strategy"])

    def test_voltage_chip_tune_strategy_true_for_bixbit(self):
        engine = _make_mock_engine("bixbit")
        self.assertTrue(get_status(engine)["capabilities"]["voltage_chip_tune_strategy"])

    def test_voltage_chip_tune_strategy_true_for_luxos(self):
        engine = _make_mock_engine("luxos")
        self.assertTrue(get_status(engine)["capabilities"]["voltage_chip_tune_strategy"])

    def test_voltage_chip_tune_strategy_false_for_braiins(self):
        engine = _make_mock_engine("braiins")
        self.assertFalse(get_status(engine)["capabilities"]["voltage_chip_tune_strategy"])

    def test_voltage_chip_tune_strategy_false_for_whatsminer(self):
        engine = _make_mock_engine("whatsminer")
        self.assertFalse(get_status(engine)["capabilities"]["voltage_chip_tune_strategy"])

    def test_power_limit_freq_search_strategy_false_for_epic(self):
        engine = _make_mock_engine("epic")
        self.assertFalse(get_status(engine)["capabilities"]["power_limit_freq_search_strategy"])

    def test_power_limit_freq_search_strategy_false_for_bixbit(self):
        engine = _make_mock_engine("bixbit")
        self.assertFalse(get_status(engine)["capabilities"]["power_limit_freq_search_strategy"])

    def test_power_limit_freq_search_strategy_false_for_luxos(self):
        engine = _make_mock_engine("luxos")
        self.assertFalse(get_status(engine)["capabilities"]["power_limit_freq_search_strategy"])

    def test_power_limit_freq_search_strategy_false_for_braiins(self):
        engine = _make_mock_engine("braiins")
        self.assertFalse(get_status(engine)["capabilities"]["power_limit_freq_search_strategy"])

    def test_power_limit_freq_search_strategy_true_for_whatsminer(self):
        engine = _make_mock_engine("whatsminer")
        self.assertTrue(get_status(engine)["capabilities"]["power_limit_freq_search_strategy"])

    def test_wattage_search_strategy_false_for_epic(self):
        engine = _make_mock_engine("epic")
        self.assertFalse(get_status(engine)["capabilities"]["wattage_search_strategy"])

    def test_wattage_search_strategy_false_for_bixbit(self):
        engine = _make_mock_engine("bixbit")
        self.assertFalse(get_status(engine)["capabilities"]["wattage_search_strategy"])

    def test_wattage_search_strategy_false_for_luxos(self):
        engine = _make_mock_engine("luxos")
        self.assertFalse(get_status(engine)["capabilities"]["wattage_search_strategy"])

    def test_wattage_search_strategy_true_for_braiins(self):
        engine = _make_mock_engine("braiins")
        self.assertTrue(get_status(engine)["capabilities"]["wattage_search_strategy"])

    def test_wattage_search_strategy_false_for_whatsminer(self):
        engine = _make_mock_engine("whatsminer")
        self.assertFalse(get_status(engine)["capabilities"]["wattage_search_strategy"])
