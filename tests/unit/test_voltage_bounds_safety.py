"""Fail-closed tests for live PSU-bound provenance and Phase 0 ordering."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tuner_app.miner.exceptions import MinerNotReady, UnsafeVoltageBoundsError
from tuner_app.miner.types import HardwareTopology, MinerSummary
from tuner_app.tuning_engine.apply import phase1_set_voltage
from tuner_app.tuning_engine.engine import TuningEngine
from tuner_app.tuning_engine.phase_runners import phase0_discovery


def _topology(*, verified: bool, num_boards: int = 3) -> HardwareTopology:
    return HardwareTopology(
        num_boards=num_boards,
        chips_per_board=108,
        psu_min_mv=11877,
        psu_max_mv=15182,
        psu_bounds_verified=verified,
        psu_bounds_source=("test:firmware-live" if verified else "fallback:static-spec"),
    )


def _phase0_engine(strategy: str, topology: HardwareTopology) -> MagicMock:
    engine = MagicMock()
    engine.PHASE_DISCOVERY = "discovery"
    engine.phase = None
    engine.phase_detail = ""
    engine.running = True
    engine.num_boards = 3
    engine.chips_per_board = 108
    engine.config = {"START_VOLTAGE_MV": 0, "CHIP_FREQ_SPREAD_MHZ": 25}
    engine.api.tuning_strategy.return_value = strategy
    engine.api.summary.return_value = MinerSummary(
        operating_state="Mining",
        hashrate_ths=200.0,
        power_w=3500.0,
        fan_speed=80.0,
    )
    engine.api.hardware_topology.return_value = topology
    return engine


def test_phase0_refuses_unverified_voltage_strategy_before_any_mutation():
    engine = _phase0_engine("voltage_chip_tune", _topology(verified=False))

    with (
        patch("tuner_app.tuning_engine.phase_runners.iter_all_config_keys", return_value=[]),
        pytest.raises(UnsafeVoltageBoundsError, match="unverified"),
    ):
        phase0_discovery(engine)

    engine._mrr_apply_pool_config.assert_not_called()
    engine._mrr_sync.assert_not_called()
    engine.api.set_perpetualtune.assert_not_called()
    engine.api.start_mining.assert_not_called()


def test_phase0_validates_summary_and_topology_before_mrr_pool_write():
    engine = _phase0_engine("voltage_chip_tune", _topology(verified=True))
    calls = MagicMock()
    calls.attach_mock(engine.api.summary, "summary")
    calls.attach_mock(engine.api.hardware_topology, "hardware_topology")
    calls.attach_mock(engine._mrr_apply_pool_config, "mrr_pool_write")

    with patch("tuner_app.tuning_engine.phase_runners.iter_all_config_keys", return_value=[]):
        phase0_discovery(engine)

    names = [call[0] for call in calls.mock_calls]
    assert names.index("summary") < names.index("hardware_topology")
    assert names.index("hardware_topology") < names.index("mrr_pool_write")
    engine._mrr_apply_pool_config.assert_called_once_with(reason="Phase 0 start")


def test_invalid_topology_blocks_mrr_pool_write():
    engine = _phase0_engine("power_limit_freq_search", _topology(verified=False, num_boards=0))

    with (
        patch("tuner_app.tuning_engine.phase_runners.iter_all_config_keys", return_value=[]),
        pytest.raises(MinerNotReady, match="hashboard count"),
    ):
        phase0_discovery(engine)

    engine._mrr_apply_pool_config.assert_not_called()
    engine._mrr_sync.assert_not_called()


@pytest.mark.parametrize("strategy", ["wattage_search", "power_limit_freq_search"])
def test_non_voltage_strategies_remain_usable_with_informational_fallback(strategy):
    engine = _phase0_engine(strategy, _topology(verified=False))

    with patch("tuner_app.tuning_engine.phase_runners.iter_all_config_keys", return_value=[]):
        phase0_discovery(engine)

    engine._mrr_apply_pool_config.assert_called_once()
    engine.api.set_perpetualtune.assert_called_once_with(False)


def test_phase1_cannot_reach_voltage_or_frequency_write_without_verified_bounds():
    engine = MagicMock()
    engine.api.tuning_strategy.return_value = "voltage_chip_tune"
    engine.voltage_topology = _topology(verified=False)

    with pytest.raises(UnsafeVoltageBoundsError, match="unverified"):
        phase1_set_voltage(engine, 14000, 490)

    engine.api.set_voltage.assert_not_called()
    engine.api.set_clock_all.assert_not_called()
    engine._wait_for_mining_state.assert_not_called()


def test_verified_bounds_still_reject_out_of_range_target():
    topology = _topology(verified=True)

    with pytest.raises(UnsafeVoltageBoundsError, match="outside verified PSU range"):
        topology.require_verified_voltage_target(16000)


def test_engine_treats_provenance_failure_as_terminal_without_recovery():
    engine = MagicMock()
    engine.running = True
    engine.PHASE_ERROR = "error"
    engine._run_inner.side_effect = UnsafeVoltageBoundsError("unverified test bounds")

    TuningEngine._run(engine)

    assert engine.phase == "error"
    assert "Safety stop" in engine.phase_detail
    engine._attempt_miner_recovery.assert_not_called()
    engine._mrr_sync.assert_not_called()
