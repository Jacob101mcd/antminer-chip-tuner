import unittest
from unittest.mock import MagicMock, patch

from tuner_app.profit.compute import score_cell
from tuner_app.tuning_engine.monitor import evaluate_chip_tune_fallback

BTC_COIN = {
    "price_usd": 42000.0,
    "reward_block": 6.25,
    "network_hashrate": 5.2e20,
    "block_time_s": 600,
}


class TestChipTuneFallback(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.engine.config = {"TARGET_MODE": "efficiency"}
        self.engine.stable_freq_arrays = [[490.0, 490.0, 490.0]]
        self.engine.voltage_results = []
        self.engine.active_sweep_voltage_mv = None
        self.engine._apply_stable_freqs = MagicMock()
        self.engine._save_profile = MagicMock()
        self.engine.log = MagicMock()

    def test_efficiency_chip_better_no_flip(self):
        # chip J/TH 18 < fine J/TH 20 => chip wins => no flip (currently_chip=True)
        active_entry = {
            "voltage_mv": 14000,
            "efficiency_jth": 18.0,
            "hashrate_ths": 200.0,
            "power_w": 3600.0,
            "fine_tune_efficiency_jth": 20.0,
            "fine_tune_hashrate_ths": 200.0,
            "fine_tune_power_w": 4000.0,
            "fine_tune_freq_arrays": [[500.0, 500.0, 500.0]],
            "stable_freq_arrays": [[490.0, 490.0, 490.0]],
            "chip_tune_active": True,
        }
        self.engine.config = {"TARGET_MODE": "efficiency"}
        evaluate_chip_tune_fallback(self.engine, active_entry)
        self.engine._apply_stable_freqs.assert_not_called()
        self.engine._save_profile.assert_not_called()
        self.engine.log.assert_not_called()

    def test_efficiency_fine_better_flip(self):
        # chip J/TH 20 > fine J/TH 18 => fine wins => FLIP (currently_chip=True)
        active_entry = {
            "voltage_mv": 14000,
            "efficiency_jth": 20.0,
            "hashrate_ths": 200.0,
            "power_w": 4000.0,
            "fine_tune_efficiency_jth": 18.0,
            "fine_tune_hashrate_ths": 200.0,
            "fine_tune_power_w": 3600.0,
            "fine_tune_freq_arrays": [[500.0, 500.0, 500.0]],
            "stable_freq_arrays": [[490.0, 490.0, 490.0]],
            "chip_tune_active": True,
        }
        self.engine.config = {"TARGET_MODE": "efficiency"}
        evaluate_chip_tune_fallback(self.engine, active_entry)
        self.assertFalse(active_entry["chip_tune_active"])
        self.engine._apply_stable_freqs.assert_called_once()
        self.engine._save_profile.assert_called_once()
        self.engine.log.assert_called_once()
        # stable_freq_arrays should be deep copy of fine_tune_freq_arrays
        self.assertEqual(self.engine.stable_freq_arrays, [[500.0, 500.0, 500.0]])

    @patch("tuner_app.tuning_engine.monitor.get_scoring_context")
    def test_profitability_mode_with_coin_data(self, mock_get_scoring_context):
        mock_get_scoring_context.return_value = ("profitability", 0.10, BTC_COIN, 0.0)
        # chip: 200 TH/s at 4000W; fine: 210 TH/s at 4300W
        # Compute expected scores to determine which variant should win
        chip_entry = {
            "efficiency_jth": 20.0,
            "hashrate_ths": 200.0,
            "power_w": 4000.0,
            "thermal_failed": False,
        }
        fine_entry = {
            "efficiency_jth": 20.48,
            "hashrate_ths": 210.0,
            "power_w": 4300.0,
            "thermal_failed": False,
        }
        chip_score = score_cell(chip_entry, "profitability", 0.10, BTC_COIN, 0.0)
        fine_score = score_cell(fine_entry, "profitability", 0.10, BTC_COIN, 0.0)
        self.assertIsNotNone(chip_score)
        self.assertIsNotNone(fine_score)
        expected_winner = "chip" if chip_score <= fine_score else "fine"
        active_entry = {
            "voltage_mv": 14000,
            "efficiency_jth": chip_entry["efficiency_jth"],
            "hashrate_ths": chip_entry["hashrate_ths"],
            "power_w": chip_entry["power_w"],
            "fine_tune_efficiency_jth": fine_entry["efficiency_jth"],
            "fine_tune_hashrate_ths": fine_entry["hashrate_ths"],
            "fine_tune_power_w": fine_entry["power_w"],
            "fine_tune_freq_arrays": [[500.0, 500.0, 500.0]],
            "stable_freq_arrays": [[490.0, 490.0, 490.0]],
            "chip_tune_active": True,
        }
        evaluate_chip_tune_fallback(self.engine, active_entry)
        if expected_winner == "fine":
            self.assertFalse(active_entry["chip_tune_active"])
            self.engine._apply_stable_freqs.assert_called_once()
            self.engine._save_profile.assert_called_once()
            self.engine.log.assert_called_once()
        else:
            self.assertTrue(active_entry["chip_tune_active"])
            self.engine._apply_stable_freqs.assert_not_called()

    @patch("tuner_app.tuning_engine.monitor.get_scoring_context")
    def test_profitability_no_coin_data_no_flip(self, mock_get_scoring_context):
        mock_get_scoring_context.return_value = ("profitability", 0.10, None, 0.0)
        active_entry = {
            "voltage_mv": 14000,
            "efficiency_jth": 20.0,
            "hashrate_ths": 200.0,
            "power_w": 4000.0,
            "fine_tune_efficiency_jth": 18.0,
            "fine_tune_hashrate_ths": 200.0,
            "fine_tune_power_w": 3600.0,
            "fine_tune_freq_arrays": [[500.0, 500.0, 500.0]],
            "stable_freq_arrays": [[490.0, 490.0, 490.0]],
            "chip_tune_active": True,
        }
        evaluate_chip_tune_fallback(self.engine, active_entry)
        self.engine._apply_stable_freqs.assert_not_called()
        self.engine.log.assert_not_called()

    def test_no_repeat_log_on_stable_state(self):
        # chip wins (18 < 20), currently_chip=True => no flip, no log, ever
        active_entry = {
            "voltage_mv": 14000,
            "efficiency_jth": 18.0,
            "hashrate_ths": 200.0,
            "power_w": 3600.0,
            "fine_tune_efficiency_jth": 20.0,
            "fine_tune_hashrate_ths": 200.0,
            "fine_tune_power_w": 4000.0,
            "fine_tune_freq_arrays": [[500.0, 500.0, 500.0]],
            "stable_freq_arrays": [[490.0, 490.0, 490.0]],
            "chip_tune_active": True,
        }
        self.engine.config = {"TARGET_MODE": "efficiency"}
        evaluate_chip_tune_fallback(self.engine, active_entry)
        evaluate_chip_tune_fallback(self.engine, active_entry)
        self.engine.log.assert_not_called()

    def test_fine_tune_freq_arrays_none_early_return(self):
        active_entry = {
            "voltage_mv": 14000,
            "efficiency_jth": 18.0,
            "fine_tune_freq_arrays": None,
            "chip_tune_active": True,
        }
        self.engine.config = {"TARGET_MODE": "efficiency"}
        evaluate_chip_tune_fallback(self.engine, active_entry)
        self.engine._apply_stable_freqs.assert_not_called()
        self.engine._save_profile.assert_not_called()
        self.engine.log.assert_not_called()
