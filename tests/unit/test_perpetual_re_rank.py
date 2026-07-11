import unittest
from unittest.mock import MagicMock

from tuner_app.tuning_engine.chip_tune_orchestration import populate_fine_tune_fields
from tuner_app.tuning_engine.monitor import re_rank_active_voltage


class TestReRankActiveVoltage(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        # _score_key() is invoked at min(...) call time and must return a key
        # callable. Using efficiency_jth keeps these tests independent of the
        # profitability-mode scoring context tested in test_chip_tune_fallback.
        self.engine._score_key = MagicMock(side_effect=lambda: lambda r: r["efficiency_jth"])
        self.engine.config = {"TARGET_MODE": "efficiency"}
        self.engine.select_voltage_profile = MagicMock()
        self.engine.log = MagicMock()

    def test_empty_voltage_results_is_noop(self):
        self.engine.voltage_results = []
        self.engine.active_sweep_voltage_mv = None
        re_rank_active_voltage(self.engine)
        self.engine.select_voltage_profile.assert_not_called()
        self.engine.log.assert_not_called()

    def test_winner_matches_active_is_noop(self):
        self.engine.voltage_results = [
            {"voltage_mv": 14000, "efficiency_jth": 17.0},
            {"voltage_mv": 13800, "efficiency_jth": 18.0},
        ]
        self.engine.active_sweep_voltage_mv = 14000
        re_rank_active_voltage(self.engine)
        self.engine.select_voltage_profile.assert_not_called()
        self.engine.log.assert_not_called()

    def test_winner_differs_switches(self):
        self.engine.voltage_results = [
            {"voltage_mv": 14000, "efficiency_jth": 18.0},
            {"voltage_mv": 13800, "efficiency_jth": 16.0},
        ]
        self.engine.active_sweep_voltage_mv = 14000
        re_rank_active_voltage(self.engine)
        self.engine.select_voltage_profile.assert_called_once_with(13800)
        self.engine.log.assert_called_once()
        msg = self.engine.log.call_args[0][0]
        self.assertIn("14000", msg)
        self.assertIn("13800", msg)
        self.assertIn("efficiency", msg)

    def test_active_None_switches_to_winner(self):
        self.engine.voltage_results = [
            {"voltage_mv": 14000, "efficiency_jth": 16.0},
            {"voltage_mv": 13800, "efficiency_jth": 17.0},
        ]
        self.engine.active_sweep_voltage_mv = None
        re_rank_active_voltage(self.engine)
        self.engine.select_voltage_profile.assert_called_once_with(14000)

    def test_select_voltage_profile_failure_caught(self):
        self.engine.voltage_results = [
            {"voltage_mv": 14000, "efficiency_jth": 16.0},
        ]
        self.engine.active_sweep_voltage_mv = 13800
        self.engine.select_voltage_profile.side_effect = ValueError("apply failed")
        re_rank_active_voltage(self.engine)
        # Both the switch-attempt log AND the failure log should fire.
        self.assertEqual(self.engine.log.call_count, 2)
        first = self.engine.log.call_args_list[0][0][0]
        second = self.engine.log.call_args_list[1][0][0]
        self.assertIn("re-rank", first)
        self.assertIn("failed (non-fatal)", second)

    def test_score_key_raises_is_noop(self):
        self.engine.voltage_results = [
            {"voltage_mv": 14000, "efficiency_jth": 16.0},
        ]
        self.engine.active_sweep_voltage_mv = 13800
        self.engine._score_key = MagicMock(side_effect=TypeError("bad context"))
        re_rank_active_voltage(self.engine)
        self.engine.select_voltage_profile.assert_not_called()
        self.engine.log.assert_not_called()

    def test_target_mode_in_log(self):
        self.engine.voltage_results = [
            {"voltage_mv": 14000, "efficiency_jth": 18.0},
            {"voltage_mv": 13800, "efficiency_jth": 16.0},
        ]
        self.engine.active_sweep_voltage_mv = 14000
        self.engine.config = {"TARGET_MODE": "profitability"}
        re_rank_active_voltage(self.engine)
        msg = self.engine.log.call_args[0][0]
        self.assertIn("profitability", msg)

    def test_target_mode_unset_defaults_to_efficiency(self):
        self.engine.voltage_results = [
            {"voltage_mv": 14000, "efficiency_jth": 18.0},
            {"voltage_mv": 13800, "efficiency_jth": 16.0},
        ]
        self.engine.active_sweep_voltage_mv = 14000
        self.engine.config = {}
        re_rank_active_voltage(self.engine)
        msg = self.engine.log.call_args[0][0]
        self.assertIn("efficiency", msg)

    def test_winner_with_no_voltage_mv_is_noop(self):
        # Defensive: malformed entry with no voltage_mv shouldn't crash.
        self.engine.voltage_results = [
            {"efficiency_jth": 14.0},
            {"voltage_mv": 14000, "efficiency_jth": 18.0},
        ]
        self.engine.active_sweep_voltage_mv = 14000
        re_rank_active_voltage(self.engine)
        self.engine.select_voltage_profile.assert_not_called()


class TestPopulateFineTuneFields(unittest.TestCase):
    def _build_engine(self, vf_surface, num_boards=3, chips_per_board=4, parked=None):
        engine = MagicMock()
        engine.vf_surface = vf_surface
        engine.num_boards = num_boards
        engine.chips_per_board = chips_per_board
        engine.stable_freq_arrays = [[500.0] * chips_per_board for _ in range(num_boards)]
        engine.parked_chips = parked or [set() for _ in range(num_boards)]
        engine.config = {"DEAD_CHIP_FREQ": 50.0}
        return engine

    def test_no_matching_vf_entry_sets_none(self):
        # vf_surface has nothing at (14000, 500.0)
        engine = self._build_engine(vf_surface=[])
        result = {}
        populate_fine_tune_fields(engine, result, 14000, 500.0)
        self.assertIsNone(result["fine_tune_freq_arrays"])
        self.assertIsNone(result["fine_tune_efficiency_jth"])
        self.assertIsNone(result["fine_tune_hashrate_ths"])
        self.assertIsNone(result["fine_tune_power_w"])

    def test_vf_entry_no_efficiency_sets_none(self):
        engine = self._build_engine(
            vf_surface=[
                {"voltage_mv": 14000, "freq_mhz": 500.0, "efficiency_jth": None},
            ]
        )
        result = {}
        populate_fine_tune_fields(engine, result, 14000, 500.0)
        self.assertIsNone(result["fine_tune_freq_arrays"])
        self.assertIsNone(result["fine_tune_efficiency_jth"])

    def test_matching_vf_entry_populates_all_fields(self):
        engine = self._build_engine(
            vf_surface=[
                {
                    "voltage_mv": 14000,
                    "freq_mhz": 500.0,
                    "efficiency_jth": 17.5,
                    "hashrate_ths": 200.0,
                    "power_w": 3500.0,
                }
            ],
            num_boards=2,
            chips_per_board=3,
        )
        result = {}
        populate_fine_tune_fields(engine, result, 14000, 500.0)
        self.assertEqual(result["fine_tune_efficiency_jth"], 17.5)
        self.assertEqual(result["fine_tune_hashrate_ths"], 200.0)
        self.assertEqual(result["fine_tune_power_w"], 3500.0)
        # Uniform seed_f_mhz across alive chips, no parked → all 500.0
        self.assertEqual(
            result["fine_tune_freq_arrays"],
            [[500.0, 500.0, 500.0], [500.0, 500.0, 500.0]],
        )

    def test_parked_chips_pinned_to_dead_freq(self):
        engine = self._build_engine(
            vf_surface=[
                {
                    "voltage_mv": 14000,
                    "freq_mhz": 500.0,
                    "efficiency_jth": 17.5,
                    "hashrate_ths": 200.0,
                    "power_w": 3500.0,
                }
            ],
            num_boards=2,
            chips_per_board=4,
            parked=[{1}, {0, 3}],
        )
        result = {}
        populate_fine_tune_fields(engine, result, 14000, 500.0)
        # Board 0: chip 1 dead. Board 1: chips 0 and 3 dead.
        self.assertEqual(
            result["fine_tune_freq_arrays"],
            [
                [500.0, 50.0, 500.0, 500.0],
                [50.0, 500.0, 500.0, 50.0],
            ],
        )

    def test_freq_key_rounded_to_three_decimals(self):
        # vf_surface_by_key uses round(freq_mhz, 3); ensure lookup matches.
        engine = self._build_engine(
            vf_surface=[
                {
                    "voltage_mv": 14000,
                    "freq_mhz": 487.5,
                    "efficiency_jth": 17.5,
                    "hashrate_ths": 200.0,
                    "power_w": 3500.0,
                }
            ],
            num_boards=1,
            chips_per_board=2,
        )
        result = {}
        populate_fine_tune_fields(engine, result, 14000, 487.5)
        self.assertEqual(result["fine_tune_efficiency_jth"], 17.5)
        self.assertEqual(result["fine_tune_freq_arrays"], [[487.5, 487.5]])

    def test_empty_stable_freq_array_falls_back_to_chips_per_board(self):
        engine = self._build_engine(
            vf_surface=[
                {
                    "voltage_mv": 14000,
                    "freq_mhz": 500.0,
                    "efficiency_jth": 17.5,
                    "hashrate_ths": 200.0,
                    "power_w": 3500.0,
                }
            ],
            num_boards=1,
            chips_per_board=5,
        )
        engine.stable_freq_arrays = [[]]  # empty board → fallback
        result = {}
        populate_fine_tune_fields(engine, result, 14000, 500.0)
        self.assertEqual(result["fine_tune_freq_arrays"], [[500.0] * 5])


if __name__ == "__main__":
    unittest.main()
