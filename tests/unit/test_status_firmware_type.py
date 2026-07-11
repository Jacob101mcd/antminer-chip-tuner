"""Tests that get_status payload includes firmware_type field."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuner_app.tuning_engine.status import get_status


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
    return engine


class TestStatusFirmwareType(unittest.TestCase):
    def test_firmware_type_in_status_epic(self):
        """get_status includes firmware_type='epic' when engine config has epic."""
        engine = _make_mock_engine("epic")
        result = get_status(engine)
        self.assertIn("firmware_type", result)
        self.assertEqual(result["firmware_type"], "epic")

    def test_firmware_type_in_status_bixbit(self):
        """get_status includes firmware_type='bixbit' when engine config has bixbit."""
        engine = _make_mock_engine("bixbit")
        result = get_status(engine)
        self.assertIn("firmware_type", result)
        self.assertEqual(result["firmware_type"], "bixbit")

    def test_firmware_type_defaults_to_epic_when_missing(self):
        """get_status defaults firmware_type to 'epic' when key absent from config."""
        engine = _make_mock_engine()
        # Remove firmware_type from config; status reads engine.firmware_type
        # directly post-A8, so also set the attribute on the mock to mirror
        # what the real TuningEngine.__init__ produces when neither v3 nor v4
        # firmware key is present.
        engine.config._data.pop("firmware_type", None)
        engine.firmware_type = "epic"
        result = get_status(engine)
        self.assertIn("firmware_type", result)
        self.assertEqual(result["firmware_type"], "epic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
