from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

from tuner_app.miner.bixbit import BixbitMinerAPI
from tuner_app.miner.braiins import BraiinsMinerAPI
from tuner_app.miner.epic import EpicMinerAPI
from tuner_app.miner.luxos import LuxosMinerAPI
from tuner_app.miner.types import HardwareTopology


class TestEpicHardwareTopology(TestCase):
    def test_epic_chips_per_board_gt_zero(self):
        api = EpicMinerAPI("1.2.3.4")
        api._capabilities_cache = {
            "Psu Info": {"Min Vout": 11877, "Max Vout": 15182},
            "Performance Estimator": {"Chip Count": 108},
            "Max HBs": 3,
        }
        result = api.hardware_topology()
        self.assertGreater(result.chips_per_board, 0)
        self.assertEqual(result.chips_per_board, 108)
        self.assertEqual(result.num_boards, 3)
        self.assertTrue(result.psu_bounds_verified)
        self.assertEqual(result.psu_bounds_source, "firmware:capabilities.Psu Info")

    def test_epic_psu_range_sane(self):
        api = EpicMinerAPI("1.2.3.4")
        api._capabilities_cache = {
            "Psu Info": {"Min Vout": 11877, "Max Vout": 15182},
            "Performance Estimator": {"Chip Count": 108},
            "Max HBs": 3,
        }
        result = api.hardware_topology()
        self.assertGreaterEqual(result.psu_min_mv, 11000)
        self.assertLessEqual(result.psu_max_mv, 16000)
        self.assertLess(result.psu_min_mv, result.psu_max_mv)

    def test_epic_fallback_on_bad_psu_range(self):
        api = EpicMinerAPI("1.2.3.4")
        api._capabilities_cache = {
            "Psu Info": {"Min Vout": 0, "Max Vout": 0},
            "Performance Estimator": {"Chip Count": 108},
            "Max HBs": 3,
        }
        result = api.hardware_topology()
        self.assertEqual(result.psu_min_mv, 11877)
        self.assertEqual(result.psu_max_mv, 15182)
        self.assertFalse(result.psu_bounds_verified)
        self.assertEqual(result.psu_bounds_source, "fallback:static-spec")


class TestBixbitHardwareTopology(TestCase):
    _MOCK_RESPONSE = {"STATUS": "S", "enabled": [True, True, True]}

    def test_bixbit_chips_per_board_is_zero(self):
        api = BixbitMinerAPI("1.2.3.4")
        api._send_cmd = MagicMock(return_value=self._MOCK_RESPONSE)
        result = api.hardware_topology()
        self.assertEqual(result.chips_per_board, 0)
        self.assertIsInstance(result, HardwareTopology)

    def test_bixbit_psu_range_sane(self):
        api = BixbitMinerAPI("1.2.3.4")
        api._send_cmd = MagicMock(return_value=self._MOCK_RESPONSE)
        result = api.hardware_topology()
        self.assertGreaterEqual(result.psu_min_mv, 11000)
        self.assertLessEqual(result.psu_max_mv, 16000)
        self.assertLess(result.psu_min_mv, result.psu_max_mv)
        self.assertFalse(result.psu_bounds_verified)

    def test_bixbit_num_boards_ge_1(self):
        api = BixbitMinerAPI("1.2.3.4")
        api._send_cmd = MagicMock(return_value={"STATUS": "S", "enabled": [True, True, True]})
        self.assertGreaterEqual(api.hardware_topology().num_boards, 1)

    def test_bixbit_hardware_topology_uses_get_board_slots_state(self):
        api = BixbitMinerAPI("1.2.3.4")
        api._send_cmd = MagicMock(return_value={"STATUS": "S", "enabled": [True, True, True, True]})
        result = api.hardware_topology()
        self.assertEqual(result.num_boards, 4)
        self.assertEqual(result.chips_per_board, 0)

    def test_bixbit_hardware_topology_falls_back_on_cmd_failure(self):
        from tuner_app.miner.exceptions import MinerCommandError

        api = BixbitMinerAPI("1.2.3.4")
        api._send_cmd = MagicMock(side_effect=MinerCommandError("offline"))
        result = api.hardware_topology()
        self.assertEqual(result.num_boards, 3)
        self.assertEqual(result.chips_per_board, 0)


class TestLuxosHardwareTopology(TestCase):
    def setUp(self):
        self.api = LuxosMinerAPI("1.2.3.4")
        self.api._transport = MagicMock()

    def test_luxos_chips_per_board_gt_zero(self):
        self.api._transport.send_cmd.side_effect = [
            {"LIMITS": [{"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}]},
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
        ]
        result = self.api.hardware_topology()
        self.assertGreater(result.chips_per_board, 0)
        self.assertEqual(result.chips_per_board, 108)

    def test_luxos_num_boards_from_devdetails(self):
        self.api._transport.send_cmd.side_effect = [
            {"LIMITS": [{"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}]},
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
        ]
        result = self.api.hardware_topology()
        self.assertEqual(result.num_boards, 3)

    def test_luxos_psu_range_sane(self):
        self.api._transport.send_cmd.side_effect = [
            {"LIMITS": [{"VoltageMin": 11.877, "VoltageMax": 15.182, "VoltageStepMin": 0.05}]},
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
        ]
        result = self.api.hardware_topology()
        self.assertGreaterEqual(result.psu_min_mv, 11000)
        self.assertLessEqual(result.psu_max_mv, 16000)
        self.assertLess(result.psu_min_mv, result.psu_max_mv)
        self.assertTrue(result.psu_bounds_verified)
        self.assertEqual(result.psu_bounds_source, "firmware:limits")

    def test_luxos_fallback_on_bad_psu_range(self):
        self.api._transport.send_cmd.side_effect = [
            {"LIMITS": [{"VoltageMin": 0.0, "VoltageMax": 0.0, "VoltageStepMin": 0.05}]},
            {"FREQS": [{"Count": 108}]},
            {"DEVDETAILS": [{}, {}, {}]},
        ]
        result = self.api.hardware_topology()
        self.assertEqual(result.psu_min_mv, 11877)
        self.assertEqual(result.psu_max_mv, 15182)
        self.assertFalse(result.psu_bounds_verified)


class TestBraiinsHardwareTopology(TestCase):
    def test_hardware_topology_braiins_with_hashboards_constraints(self):
        mock_json = {"hashboards_constraints": {"count": 4}}
        with patch.object(BraiinsMinerAPI, "_get_json", return_value=mock_json):
            api = BraiinsMinerAPI("1.2.3.4")
            result = api.hardware_topology()
            self.assertEqual(result.num_boards, 4)
            self.assertEqual(result.chips_per_board, 0)
            self.assertIsInstance(result, HardwareTopology)
            self.assertFalse(result.psu_bounds_verified)
            self.assertEqual(result.psu_bounds_source, "not-applicable:firmware-owned-vf")

    def test_hardware_topology_braiins_falls_back_when_constraints_missing(self):
        mock_json = {}
        with patch.object(BraiinsMinerAPI, "_get_json", return_value=mock_json):
            api = BraiinsMinerAPI("1.2.3.4")
            result = api.hardware_topology()
            self.assertEqual(result.num_boards, 3)
            self.assertEqual(result.chips_per_board, 0)
            self.assertEqual(result.psu_min_mv, 11877)
            self.assertEqual(result.psu_max_mv, 15182)

    def test_hardware_topology_braiins_falls_back_when_hashboards_constraints_none(self):
        mock_json = {"hashboards_constraints": None}
        with patch.object(BraiinsMinerAPI, "_get_json", return_value=mock_json):
            api = BraiinsMinerAPI("1.2.3.4")
            result = api.hardware_topology()
            self.assertEqual(result.num_boards, 3)

    def test_hardware_topology_braiins_caches_result(self):
        mock_json = {"hashboards_constraints": {"count": 4}}
        with patch.object(BraiinsMinerAPI, "_get_json", return_value=mock_json) as mock_get_json:
            api = BraiinsMinerAPI("1.2.3.4")
            api.hardware_topology()
            api.hardware_topology()
            self.assertEqual(mock_get_json.call_count, 1)
