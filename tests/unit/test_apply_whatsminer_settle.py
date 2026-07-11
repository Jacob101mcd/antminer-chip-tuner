from __future__ import annotations

from unittest.mock import MagicMock

from tuner_app.tuning_engine.apply import (
    wait_for_upfreq_complete,
    wait_for_whatsminer_restart,
    wait_for_whatsminer_stable,
)


def _make_engine():
    engine = MagicMock()
    engine.running = True
    engine._destroyed = False
    engine.config = {
        "WHATSMINER_STABILIZE_SEC": 0,
        "WHATSMINER_UPFREQ_TIMEOUT_SEC": 5,
        "WHATSMINER_RESTART_WAIT_SEC": 5,
    }
    engine.api = MagicMock()
    return engine


def test_wait_for_upfreq_complete_returns_true_when_all_complete():
    engine = _make_engine()
    engine.api.devs.return_value = {
        "DEVS": [
            {"Upfreq Complete": 1},
            {"Upfreq Complete": 1},
            {"Upfreq Complete": 1},
        ]
    }
    assert wait_for_upfreq_complete(engine) is True


def test_wait_for_upfreq_complete_waits_until_all_complete():
    engine = _make_engine()
    engine.api.devs.side_effect = [
        {"DEVS": [{"Upfreq Complete": 0}, {"Upfreq Complete": 1}]},
        {"DEVS": [{"Upfreq Complete": 0}, {"Upfreq Complete": 0}]},
        {"DEVS": [{"Upfreq Complete": 1}, {"Upfreq Complete": 1}]},
    ]
    assert wait_for_upfreq_complete(engine) is True


def test_wait_for_upfreq_complete_returns_false_on_timeout():
    engine = _make_engine()
    engine.config["WHATSMINER_UPFREQ_TIMEOUT_SEC"] = 2
    engine.api.devs.return_value = {"DEVS": [{"Upfreq Complete": 0}]}
    assert wait_for_upfreq_complete(engine, timeout_sec=2) is False


def test_wait_for_upfreq_complete_variable_board_count_1():
    engine = _make_engine()
    engine.api.devs.return_value = {"DEVS": [{"Upfreq Complete": 1}]}
    assert wait_for_upfreq_complete(engine) is True


def test_wait_for_upfreq_complete_variable_board_count_4():
    engine = _make_engine()
    engine.api.devs.return_value = {
        "DEVS": [
            {"Upfreq Complete": 1},
            {"Upfreq Complete": 1},
            {"Upfreq Complete": 1},
            {"Upfreq Complete": 1},
        ]
    }
    assert wait_for_upfreq_complete(engine) is True


def test_wait_for_upfreq_complete_returns_false_when_engine_stopped():
    engine = _make_engine()
    engine.running = False
    engine.api.devs.return_value = {"DEVS": [{"Upfreq Complete": 0}]}
    assert wait_for_upfreq_complete(engine) is False


def test_wait_for_upfreq_complete_returns_false_when_destroyed():
    engine = _make_engine()
    engine._destroyed = True
    engine.api.devs.return_value = {"DEVS": [{"Upfreq Complete": 0}]}
    assert wait_for_upfreq_complete(engine) is False


def test_wait_for_whatsminer_stable_calls_upfreq_then_sleeps():
    engine = _make_engine()
    engine.config["WHATSMINER_STABILIZE_SEC"] = 0
    engine.api.devs.return_value = {"DEVS": [{"Upfreq Complete": 1}]}
    assert wait_for_whatsminer_stable(engine) is None


def test_wait_for_whatsminer_restart_returns_true_when_summary_ok():
    engine = _make_engine()
    summary = MagicMock()
    summary.operating_state = "Mining"
    engine.api.summary.return_value = summary
    assert wait_for_whatsminer_restart(engine) is True


def test_wait_for_whatsminer_restart_handles_connection_refused():
    engine = _make_engine()
    engine.config["WHATSMINER_RESTART_WAIT_SEC"] = 5
    engine.api.summary.side_effect = [
        ConnectionRefusedError,
        TimeoutError,
        MagicMock(operating_state="Idle"),
    ]
    assert wait_for_whatsminer_restart(engine) is True


def test_wait_for_whatsminer_restart_handles_timeout():
    engine = _make_engine()
    engine.config["WHATSMINER_RESTART_WAIT_SEC"] = 2
    engine.api.summary.side_effect = ConnectionRefusedError
    assert wait_for_whatsminer_restart(engine) is False
