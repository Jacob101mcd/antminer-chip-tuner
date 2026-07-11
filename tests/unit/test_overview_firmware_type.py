"""Tests that get_overview() per-miner row includes firmware_type.

Run 7 added firmware_type to the miners[] row so the fleet table (and future
per-miner config UI) can render vendor-specific UI hints without needing a
separate /tuner/status fetch.

Coverage:
- firmware_type='epic' propagated from engine status to overview row
- firmware_type='bixbit' propagated from engine status to overview row
- firmware_type absent from engine status defaults to 'epic' in overview row
- mixed-firmware fleet (epic + bixbit) — each row carries its own value
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.manager.tuner_manager import TunerManager


def _make_engine_status(firmware_type="epic"):
    """Minimal get_status() return value for overview consumption."""
    return {
        "phase": "idle",
        "phase_detail": "",
        "tuned_stats": {},
        "firmware_type": firmware_type,
        "avg_board_temp_c": None,
        "avg_chip_temp_c": None,
        "active_sweep_voltage_mv": None,
        "sweep_voltage_mv": None,
        "tuning_complete": False,
        "engine_busy": False,
        "offline_since_ts": None,
        "last_successful_contact_ts": None,
        "mrr_last_sync": None,
    }


def _make_mock_engine(firmware_type="epic"):
    """Minimal mock TuningEngine that satisfies get_overview's callers."""
    engine = MagicMock()
    engine.get_status.return_value = _make_engine_status(firmware_type)
    engine.last_summary = {}
    engine._get_profit_display_context.return_value = (0.10, None, 0.0)
    return engine


class TestOverviewFirmwareType(unittest.TestCase):
    def setUp(self):
        import copy

        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov

    def _manager_with_engines(self, ip_firmware_pairs):
        """Return a TunerManager whose engines dict is pre-populated with mocks."""
        manager = TunerManager(state.CONFIG)
        ips = []
        for ip, firmware_type in ip_firmware_pairs:
            ips.append(ip)
            manager.engines[ip] = _make_mock_engine(firmware_type)
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ips
        return manager

    def _find_miner(self, overview, ip):
        return next((m for m in overview["miners"] if m["ip"] == ip), None)

    def test_epic_firmware_type_in_overview_row(self):
        """get_overview row for an ePIC miner carries firmware_type='epic'."""
        manager = self._manager_with_engines([("10.0.0.1", "epic")])
        overview = manager.get_overview()
        row = self._find_miner(overview, "10.0.0.1")
        self.assertIsNotNone(row, "miner row missing from overview")
        self.assertIn("firmware_type", row)
        self.assertEqual(row["firmware_type"], "epic")

    def test_bixbit_firmware_type_in_overview_row(self):
        """get_overview row for a Bixbit miner carries firmware_type='bixbit'."""
        manager = self._manager_with_engines([("10.0.0.2", "bixbit")])
        overview = manager.get_overview()
        row = self._find_miner(overview, "10.0.0.2")
        self.assertIsNotNone(row, "miner row missing from overview")
        self.assertIn("firmware_type", row)
        self.assertEqual(row["firmware_type"], "bixbit")

    def test_firmware_type_required_in_status(self):
        """get_overview raises KeyError when status dict lacks firmware_type.

        firmware_type is a required field in the engine status payload since R1.
        The overview row reads it with direct subscript (no fallback) so that
        a future regression that drops the field fails loudly rather than
        silently returning 'epic' for every miner.
        """
        manager = self._manager_with_engines([("10.0.0.3", "epic")])
        # Remove firmware_type from the status return value to simulate a
        # regression where get_status() drops the field.
        status = _make_engine_status("epic")
        del status["firmware_type"]
        manager.engines["10.0.0.3"].get_status.return_value = status

        with self.assertRaises(KeyError):
            manager.get_overview()

    def test_mixed_fleet_each_row_carries_own_firmware_type(self):
        """Mixed fleet: each miner row carries its own firmware_type independently."""
        manager = self._manager_with_engines(
            [
                ("10.0.0.10", "epic"),
                ("10.0.0.11", "bixbit"),
                ("10.0.0.12", "epic"),
            ]
        )
        overview = manager.get_overview()

        row_epic1 = self._find_miner(overview, "10.0.0.10")
        row_bixbit = self._find_miner(overview, "10.0.0.11")
        row_epic2 = self._find_miner(overview, "10.0.0.12")

        self.assertEqual(row_epic1["firmware_type"], "epic")
        self.assertEqual(row_bixbit["firmware_type"], "bixbit")
        self.assertEqual(row_epic2["firmware_type"], "epic")

    def test_firmware_type_positioned_near_model_in_row(self):
        """firmware_type key is present alongside model and hostname in the row dict."""
        manager = self._manager_with_engines([("10.0.0.20", "epic")])
        overview = manager.get_overview()
        row = self._find_miner(overview, "10.0.0.20")
        # All three sibling keys must coexist in the same dict
        for key in ("ip", "hostname", "model", "firmware_type"):
            self.assertIn(key, row, f"expected key '{key}' in overview miner row")


if __name__ == "__main__":
    unittest.main(verbosity=2)
