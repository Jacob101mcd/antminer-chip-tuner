from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tuner_app.miner.types import HardwareTopology
from tuner_app.tuning_engine.apply import (
    apply_freqs_direct,
    apply_stable_freqs,
    wait_for_per_chip_freqs_settle,
)


def test_returns_immediately_when_per_chip_unsupported():
    engine = MagicMock()
    engine.api.supports_per_chip_tuning.return_value = False
    engine.api.clocks = MagicMock()
    result = wait_for_per_chip_freqs_settle(engine, [[500.0] * 108] * 3)
    assert result is None
    assert not engine.api.clocks.called


def test_returns_when_all_chips_at_target():
    engine = MagicMock()
    engine.api.supports_per_chip_tuning.return_value = True
    engine.parked_chips = [set(), set(), set()]
    engine.running = True
    engine.phase_detail = ""
    engine.config = {"SETTLE_MAX_ATTEMPTS": 20, "SETTLE_POLL_INTERVAL": 0.0}
    engine.api.clocks = MagicMock()
    engine.api.clocks.return_value = [
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
    ]
    with patch("tuner_app.tuning_engine.apply.time.sleep", lambda x: None):
        result = wait_for_per_chip_freqs_settle(
            engine, [[500.0] * 108, [500.0] * 108, [500.0] * 108]
        )
    assert result is None
    assert engine.api.clocks.call_count == 1
    assert not engine.api.set_clock_chip.called


def test_polls_until_all_chips_within_tolerance():
    engine = MagicMock()
    engine.api.supports_per_chip_tuning.return_value = True
    engine.parked_chips = [set(), set(), set()]
    engine.running = True
    engine.phase_detail = ""
    engine.config = {"SETTLE_MAX_ATTEMPTS": 20, "SETTLE_POLL_INTERVAL": 0.0}
    engine.api.clocks = MagicMock()
    engine.api.clocks.side_effect = [
        [SimpleNamespace(chip_freqs_mhz=[480.0] + [500.0] * 107)],
        [SimpleNamespace(chip_freqs_mhz=[495.0] + [500.0] * 107)],
    ]
    with patch("tuner_app.tuning_engine.apply.time.sleep", lambda x: None):
        result = wait_for_per_chip_freqs_settle(
            engine, [[500.0] * 108, [500.0] * 108, [500.0] * 108]
        )
    assert result is None
    assert engine.api.clocks.call_count == 2


def test_skips_parked_chips():
    engine = MagicMock()
    engine.api.supports_per_chip_tuning.return_value = True
    engine.parked_chips = [{5, 10}, set(), set()]
    engine.running = True
    engine.phase_detail = ""
    engine.config = {"SETTLE_MAX_ATTEMPTS": 20, "SETTLE_POLL_INTERVAL": 0.0}
    engine.api.clocks = MagicMock()
    engine.api.clocks.return_value = [
        SimpleNamespace(chip_freqs_mhz=[500.0] * 5 + [50.0] + [500.0] * 5 + [500.0] * 98),
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
    ]
    with patch("tuner_app.tuning_engine.apply.time.sleep", lambda x: None):
        result = wait_for_per_chip_freqs_settle(engine, [[500.0] * 108] * 3)
    assert result is None
    assert engine.api.clocks.call_count == 1


def test_timeout_logs_and_returns_no_exception():
    engine = MagicMock()
    engine.api.supports_per_chip_tuning.return_value = True
    engine.parked_chips = [set(), set(), set()]
    engine.running = True
    engine.phase_detail = ""
    engine.config = {"SETTLE_MAX_ATTEMPTS": 3, "SETTLE_POLL_INTERVAL": 0.0}
    engine.api.clocks = MagicMock()
    engine.api.clocks.return_value = [
        SimpleNamespace(chip_freqs_mhz=[400.0] + [500.0] * 107),
        SimpleNamespace(chip_freqs_mhz=[400.0] + [500.0] * 107),
        SimpleNamespace(chip_freqs_mhz=[400.0] + [500.0] * 107),
    ]
    engine.log = MagicMock()
    with patch("tuner_app.tuning_engine.apply.time.sleep", lambda x: None):
        result = wait_for_per_chip_freqs_settle(engine, [[500.0] * 108] * 3)
    assert result is None
    assert engine.api.clocks.call_count == 3
    assert any("timeout" in str(call[0]).lower() for call in engine.log.call_args_list)


def test_tolerates_empty_parked_chips():
    engine = MagicMock()
    engine.api.supports_per_chip_tuning.return_value = True
    engine.parked_chips = []
    engine.running = True
    engine.phase_detail = ""
    engine.config = {"SETTLE_MAX_ATTEMPTS": 20, "SETTLE_POLL_INTERVAL": 0.0}
    engine.api.clocks = MagicMock()
    engine.api.clocks.return_value = [
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
        SimpleNamespace(chip_freqs_mhz=[500.0] * 108),
    ]
    with patch("tuner_app.tuning_engine.apply.time.sleep", lambda x: None):
        result = wait_for_per_chip_freqs_settle(engine, [[500.0] * 108] * 3)
    assert result is None

    engine.parked_chips = None
    with patch("tuner_app.tuning_engine.apply.time.sleep", lambda x: None):
        result = wait_for_per_chip_freqs_settle(engine, [[500.0] * 108] * 3)
    assert result is None


def test_apply_freqs_direct_calls_per_chip_settle_after_writes():
    engine = MagicMock()
    engine.num_boards = 3
    engine.api.supports_per_chip_tuning.return_value = True
    engine.api.set_clock_chip = MagicMock()
    parent = MagicMock()
    mock_wait = MagicMock()
    parent.attach_mock(engine.api.set_clock_chip, "set_clock_chip")
    parent.attach_mock(mock_wait, "wait_for_per_chip_freqs_settle")
    with patch("tuner_app.tuning_engine.apply.wait_for_per_chip_freqs_settle", mock_wait):
        apply_freqs_direct(engine, [[500.0, 510.0], [], [510.0]])
    calls = [c[0] for c in parent.mock_calls]
    assert calls.index("set_clock_chip") < calls.index("wait_for_per_chip_freqs_settle")
    assert engine.api.set_clock_chip.call_count == 2
    assert mock_wait.call_count == 1


def test_apply_freqs_direct_skips_settle_for_bixbit():
    engine = MagicMock()
    engine.num_boards = 3
    engine.api.supports_per_chip_tuning.return_value = False
    engine.api.set_clock_chip = MagicMock()
    mock_wait = MagicMock()
    with patch("tuner_app.tuning_engine.apply.wait_for_per_chip_freqs_settle", mock_wait):
        apply_freqs_direct(engine, [[500.0] * 108] * 3)
    assert engine.api.set_clock_chip.call_count == 3
    assert mock_wait.call_count == 1


def test_apply_stable_freqs_decreasing_calls_voltage_settle_after_set_voltage():
    engine = MagicMock()
    engine.api.tuning_strategy.return_value = "voltage_chip_tune"
    engine.voltage_topology = HardwareTopology(
        num_boards=3,
        chips_per_board=108,
        psu_min_mv=11877,
        psu_max_mv=15182,
        psu_bounds_verified=True,
        psu_bounds_source="test:verified-live-bounds",
    )
    engine.min_voltage_mv = 13000
    engine._get_current_voltage_mv.return_value = 14000
    engine.stable_freq_arrays = [[500.0] * 108] * 3
    engine.num_boards = 3
    engine.api.set_voltage = MagicMock()
    parent = MagicMock()
    mock_wait = MagicMock()
    parent.attach_mock(engine.api.set_voltage, "set_voltage")
    parent.attach_mock(mock_wait, "wait_for_voltage_settle")
    with (
        patch("tuner_app.tuning_engine.apply.wait_for_voltage_settle", mock_wait),
        patch("tuner_app.tuning_engine.apply.wait_for_clock_settle", MagicMock()),
        patch("tuner_app.tuning_engine.apply.wait_for_per_chip_freqs_settle", MagicMock()),
        patch("tuner_app.tuning_engine.apply.apply_freqs_direct", MagicMock()),
    ):
        apply_stable_freqs(engine)
    calls = [c[0] for c in parent.mock_calls]
    assert calls.index("set_voltage") < calls.index("wait_for_voltage_settle")
