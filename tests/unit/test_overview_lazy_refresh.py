"""Tests that get_overview() lazy-refreshes engine.last_summary when None.

After a tuner_app restart, engines are created in PHASE_IDLE with
last_summary=None and no auto-start of the tuning thread. Without lazy
refresh, the fleet table would render em-dashes for hostname/model on
every miner until the operator manually clicks Start. This file pins the
fix that calls engine._update_live_data() in get_overview() when
last_summary is None, mirroring the get_live_data pattern in status.py.

Coverage:
- last_summary=None triggers _update_live_data; hostname/model populate
- last_summary populated already: _update_live_data NOT called (cached read)
- _update_live_data raises MinerOfflineError: row safely renders None fields
- _update_live_data raises MinerCommandError: row safely renders None fields
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.manager.tuner_manager import TunerManager
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.types import MinerSummary


def _make_engine_status():
    return {
        "phase": "idle",
        "phase_detail": "",
        "tuned_stats": {},
        "firmware_type": "epic",
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


def _make_summary(hostname="miner-example", model="Antminer S21"):
    return MinerSummary(
        operating_state="Mining",
        hashrate_ths=200.0,
        power_w=3500.0,
        fan_speed=55,
        target_voltage_mv=14000.0,
        output_voltage_mv=14000.0,
        hostname=hostname,
        model=model,
    )


def _make_mock_engine():
    engine = MagicMock()
    engine.get_status.return_value = _make_engine_status()
    engine.last_summary = None
    engine._get_profit_display_context.return_value = (0.10, None, 0.0)
    return engine


class TestOverviewLazyRefresh(unittest.TestCase):
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

    def _manager_with_engine(self, ip, engine):
        manager = TunerManager(state.CONFIG)
        manager.engines[ip] = engine
        state.CONFIG["fleet_ops"]["MINER_IPS"] = [ip]
        return manager

    def _find_miner(self, overview, ip):
        return next((m for m in overview["miners"] if m["ip"] == ip), None)

    def test_none_summary_triggers_refresh_and_populates(self):
        """last_summary=None → _update_live_data called → hostname/model populate."""
        engine = _make_mock_engine()
        # Simulate _update_live_data populating last_summary on call
        summary = _make_summary()

        def _refresh():
            engine.last_summary = summary

        engine._update_live_data.side_effect = _refresh

        manager = self._manager_with_engine("10.0.0.1", engine)
        overview = manager.get_overview()

        engine._update_live_data.assert_called_once()
        row = self._find_miner(overview, "10.0.0.1")
        self.assertEqual(row["hostname"], "miner-example")
        self.assertEqual(row["model"], "Antminer S21")
        self.assertEqual(row["operating_state"], "Mining")

    def test_populated_summary_skips_refresh(self):
        """last_summary already set → _update_live_data NOT called."""
        engine = _make_mock_engine()
        engine.last_summary = _make_summary(hostname="prefilled", model="S21-Pre")

        manager = self._manager_with_engine("10.0.0.2", engine)
        overview = manager.get_overview()

        engine._update_live_data.assert_not_called()
        row = self._find_miner(overview, "10.0.0.2")
        self.assertEqual(row["hostname"], "prefilled")
        self.assertEqual(row["model"], "S21-Pre")

    def test_offline_error_swallowed_row_renders_none(self):
        """_update_live_data raises MinerOfflineError → row hostname/model are None."""
        engine = _make_mock_engine()
        engine._update_live_data.side_effect = MinerOfflineError("connection refused")

        manager = self._manager_with_engine("10.0.0.3", engine)
        overview = manager.get_overview()  # must not raise

        row = self._find_miner(overview, "10.0.0.3")
        self.assertIsNone(row["hostname"])
        self.assertIsNone(row["model"])

    def test_command_error_swallowed_row_renders_none(self):
        """_update_live_data raises MinerCommandError → row hostname/model are None."""
        engine = _make_mock_engine()
        engine._update_live_data.side_effect = MinerCommandError("/summary: 500")

        manager = self._manager_with_engine("10.0.0.4", engine)
        overview = manager.get_overview()  # must not raise

        row = self._find_miner(overview, "10.0.0.4")
        self.assertIsNone(row["hostname"])
        self.assertIsNone(row["model"])

    def test_refresh_runs_before_get_status_for_fresh_tuned_stats(self):
        """Order matters: _update_live_data must run BEFORE engine.get_status()
        so tuned_stats (populated from last_summary inside get_status) sees the
        freshly-fetched DTO. Reversing the order silently drops hashrate/power/
        voltage to zero on every idle engine — even though hostname/model still
        populate via the post-refresh `summary` read."""
        call_order = []
        engine = _make_mock_engine()
        engine._update_live_data.side_effect = lambda: call_order.append("refresh")
        # Wrap get_status so we record when it's called without losing its return
        original_get_status = engine.get_status

        def _record_status():
            call_order.append("status")
            return original_get_status()

        engine.get_status = _record_status

        manager = self._manager_with_engine("10.0.0.5", engine)
        manager.get_overview()

        self.assertEqual(call_order, ["refresh", "status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
