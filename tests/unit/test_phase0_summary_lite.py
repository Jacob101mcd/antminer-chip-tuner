"""Tests that ``phase0_discovery`` reuses the first ``summary()`` result
to decide whether to refresh state / call ``start_mining``, and that
recovery polling paths use ``summary_lite()`` instead of the full
``summary()``. The change cuts ~25 TCP commands per Phase 0 entry on
LuxOS where each ``summary()`` fans out to 10 sequential cmds.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tuner_app.miner.types import HardwareTopology, MinerSummary


class _FakeConfig:
    def __init__(self, overrides=None):
        self._data = {
            "API_PORT": 4028,
            "PASSWORD": "letmein",
            "firmware_type": "epic",
            "START_VOLTAGE_MV": 0,
            "CHIP_FREQ_SPREAD_MHZ": 25,
        }
        if overrides:
            self._data.update(overrides)

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data


def _make_engine():
    from tuner_app.tuning_engine.engine import TuningEngine

    cfg = _FakeConfig()
    with (
        patch("tuner_app.tuning_engine.engine.persistence.restore_saved_state"),
        patch("tuner_app.tuning_engine.engine.logging_.load_log_from_disk"),
    ):
        engine = TuningEngine("192.0.2.99", cfg)
    engine.running = True
    engine.api = MagicMock()
    engine.log = MagicMock()
    engine.config_snapshot = {}
    engine._mrr_apply_pool_config = MagicMock()
    engine._mrr_sync = MagicMock()
    engine._capture_live_stock_baseline = MagicMock()
    engine._resize_board_arrays = MagicMock()
    engine._wait_for_mining_state = MagicMock()
    return engine


class TestPhase0ReusesSummaryWhenMining(unittest.TestCase):
    def test_skips_second_summary_when_first_says_mining(self):
        from tuner_app.tuning_engine import phase_runners

        engine = _make_engine()
        # First summary: Mining. The second-summary refresh and start_mining
        # must both be skipped — they're the load-shedding optimization.
        first = MinerSummary(operating_state="Mining", hashrate_ths=200.0, power_w=0.0, fan_speed=0)
        engine.api.summary = MagicMock(return_value=first)
        engine.api.summary_lite = MagicMock()
        engine.api.start_mining = MagicMock()
        engine.api.hardware_topology = MagicMock(
            return_value=HardwareTopology(
                num_boards=3, chips_per_board=108, psu_min_mv=11877, psu_max_mv=15182
            )
        )
        engine.api.set_perpetualtune = MagicMock()

        with patch.object(phase_runners, "iter_all_config_keys", return_value=()):
            phase_runners.phase0_discovery(engine)

        # The single summary at the top of phase0_discovery must fire exactly once.
        self.assertEqual(engine.api.summary.call_count, 1)
        # No second-summary refresh, no start_mining.
        engine.api.summary_lite.assert_not_called()
        engine.api.start_mining.assert_not_called()

    def test_skips_second_summary_when_first_says_initializing(self):
        from tuner_app.tuning_engine import phase_runners

        engine = _make_engine()
        first = MinerSummary(
            operating_state="Initializing", hashrate_ths=0.0, power_w=0.0, fan_speed=0
        )
        engine.api.summary = MagicMock(return_value=first)
        engine.api.summary_lite = MagicMock()
        engine.api.start_mining = MagicMock()
        engine.api.hardware_topology = MagicMock(
            return_value=HardwareTopology(
                num_boards=3, chips_per_board=108, psu_min_mv=11877, psu_max_mv=15182
            )
        )
        engine.api.set_perpetualtune = MagicMock()

        with patch.object(phase_runners, "iter_all_config_keys", return_value=()):
            phase_runners.phase0_discovery(engine)

        engine.api.summary_lite.assert_not_called()
        engine.api.start_mining.assert_not_called()

    def test_uses_summary_lite_when_first_says_idle(self):
        from tuner_app.tuning_engine import phase_runners

        engine = _make_engine()
        first = MinerSummary(operating_state="Idle", hashrate_ths=0.0, power_w=0.0, fan_speed=0)
        # The refreshed read after set_perpetualtune sees Mining (firmware
        # transitioned in the meantime); start_mining must NOT fire.
        refreshed = MinerSummary(
            operating_state="Mining", hashrate_ths=180.0, power_w=0.0, fan_speed=0
        )
        engine.api.summary = MagicMock(return_value=first)
        engine.api.summary_lite = MagicMock(return_value=refreshed)
        engine.api.start_mining = MagicMock()
        engine.api.hardware_topology = MagicMock(
            return_value=HardwareTopology(
                num_boards=3, chips_per_board=108, psu_min_mv=11877, psu_max_mv=15182
            )
        )
        engine.api.set_perpetualtune = MagicMock()

        with patch.object(phase_runners, "iter_all_config_keys", return_value=()):
            phase_runners.phase0_discovery(engine)

        # summary_lite is the LITE path (1 cmd on LuxOS). It must be the
        # method used for the refresh — full summary() would re-fan-out 10 cmds.
        engine.api.summary_lite.assert_called_once()
        # Summary returns Mining on refresh, so start_mining stays unused.
        engine.api.start_mining.assert_not_called()

    def test_calls_start_mining_when_idle_and_remains_idle(self):
        from tuner_app.tuning_engine import phase_runners

        engine = _make_engine()
        first = MinerSummary(operating_state="Idle", hashrate_ths=0.0, power_w=0.0, fan_speed=0)
        refreshed = MinerSummary(operating_state="Idle", hashrate_ths=0.0, power_w=0.0, fan_speed=0)
        engine.api.summary = MagicMock(return_value=first)
        engine.api.summary_lite = MagicMock(return_value=refreshed)
        engine.api.start_mining = MagicMock()
        engine.api.hardware_topology = MagicMock(
            return_value=HardwareTopology(
                num_boards=3, chips_per_board=108, psu_min_mv=11877, psu_max_mv=15182
            )
        )
        engine.api.set_perpetualtune = MagicMock()

        with patch.object(phase_runners, "iter_all_config_keys", return_value=()):
            phase_runners.phase0_discovery(engine)

        engine.api.start_mining.assert_called_once()


class TestRecoveryPollingUsesSummaryLite(unittest.TestCase):
    """The recovery poll loops (wait_for_miner_online,
    wait_for_mining_state) read only operating_state and/or hashrate_ths.
    They must use ``summary_lite()`` rather than ``summary()`` to avoid
    storming the firmware with 10 TCP cmds per poll on LuxOS.
    """

    def test_wait_for_miner_online_calls_summary_lite(self):
        from tuner_app.tuning_engine import recovery

        engine = MagicMock()
        engine.running = True
        engine.config = {"OFFLINE_POLL_INTERVAL": 30}
        engine.offline_since_ts = None
        engine.pre_offline_phase = engine.PHASE_DISCOVERY
        engine.pre_offline_phase_detail = ""
        # First call: returns a summary so the poll exits.
        engine.api.summary_lite = MagicMock(
            return_value=MinerSummary(
                operating_state="Mining", hashrate_ths=200.0, power_w=0.0, fan_speed=0
            )
        )
        engine.api.summary = MagicMock(side_effect=AssertionError("must not call full summary()"))
        with patch("time.sleep"):
            recovery.wait_for_miner_online(engine)
        engine.api.summary_lite.assert_called()
        engine.api.summary.assert_not_called()

    def test_wait_for_mining_state_calls_summary_lite(self):
        from tuner_app.tuning_engine import recovery

        engine = MagicMock()
        engine.running = True
        # Returns Mining + hashrate → exit cleanly.
        s = MinerSummary(operating_state="Mining", hashrate_ths=200.0, power_w=0.0, fan_speed=0)
        engine.api.summary_lite = MagicMock(return_value=s)
        engine.api.summary = MagicMock(side_effect=AssertionError("must not call full summary()"))
        with patch("time.sleep"):
            recovery.wait_for_mining_state(engine, timeout=60)
        engine.api.summary_lite.assert_called()
        engine.api.summary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
