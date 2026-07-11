"""Tests for Run 10a: log level field + LOG_STDOUT_LEVEL gate."""

from __future__ import annotations

import collections
import threading
from unittest.mock import Mock

import pytest

from tuner_app.config.defaults import CONFIG_DEFAULTS
from tuner_app.config.validation import validate_config
from tuner_app.tuning_engine.logging_ import _should_emit_to_stdout, log

# ---------------------------------------------------------------------------
# _should_emit_to_stdout truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry, threshold, expected",
    [
        ({"level": "DEBUG"}, "DEBUG", True),
        ({"level": "DEBUG"}, "INFO", False),
        ({"level": "INFO"}, "INFO", True),
        ({"level": "WARN"}, "INFO", True),
        ({"level": "ERROR"}, "WARN", True),
        ({"level": "DEBUG"}, "ERROR", False),
    ],
)
def test_should_emit_to_stdout_truth_table(entry, threshold, expected):
    assert _should_emit_to_stdout(entry, threshold) == expected


def test_should_emit_to_stdout_missing_level_defaults_to_info():
    """Entry without 'level' field is treated as INFO (rank 1)."""
    entry = {"msg": "test"}
    assert _should_emit_to_stdout(entry, "INFO") is True
    assert _should_emit_to_stdout(entry, "WARN") is False


def test_should_emit_to_stdout_unknown_threshold_defaults_to_info():
    """Unknown threshold string defaults to INFO rank (1). WARN(2) >= INFO(1) -> True."""
    entry = {"level": "WARN"}
    assert _should_emit_to_stdout(entry, "BOGUS") is True


# ---------------------------------------------------------------------------
# Minimal stub engine factory
# ---------------------------------------------------------------------------


def _make_stub_engine(dedup_window=5):
    engine = Mock()
    engine.ip = "1.2.3.4"
    engine.current_sweep_voltage_mv = 14000
    engine.phase = "IDLE"
    engine.log_lines = collections.deque(maxlen=100)
    engine._destroyed = True  # skip disk writes
    engine.config = {"LOG_STDOUT_LEVEL": "INFO", "LOG_DEDUP_WINDOW_SEC": dedup_window}
    engine.log_file_lock = threading.Lock()
    engine._log_appends_since_rotate_check = 0
    engine.LOG_ROTATE_CHECK_INTERVAL = 50
    # Dedup state fields (Run 10c)
    engine._log_dedup_msg = None
    engine._log_dedup_level = None
    engine._log_dedup_first_ts = None
    engine._log_dedup_count = 0
    return engine


# ---------------------------------------------------------------------------
# log() level field persistence
# ---------------------------------------------------------------------------


def test_log_stores_level_in_entry():
    engine = _make_stub_engine()
    log(engine, "test message", "WARN")
    assert engine.log_lines[-1]["level"] == "WARN"


def test_log_default_level_is_info():
    engine = _make_stub_engine()
    log(engine, "default level")
    assert engine.log_lines[-1]["level"] == "INFO"


def test_log_invalid_level_fallback_to_info():
    engine = _make_stub_engine()
    log(engine, "bad level", "VERBOSE")
    assert engine.log_lines[-1]["level"] == "INFO"


def test_log_level_case_normalized_to_upper():
    engine = _make_stub_engine()
    log(engine, "lower level", "warn")
    assert engine.log_lines[-1]["level"] == "WARN"


# ---------------------------------------------------------------------------
# CONFIG_DEFAULTS
# ---------------------------------------------------------------------------


def test_config_defaults_log_stdout_level_is_info():
    assert CONFIG_DEFAULTS["LOG_STDOUT_LEVEL"] == "INFO"


# ---------------------------------------------------------------------------
# validate_config for LOG_STDOUT_LEVEL
# ---------------------------------------------------------------------------


def test_validate_config_log_stdout_level_valid():
    cleaned, errors = validate_config({"LOG_STDOUT_LEVEL": "DEBUG"})
    assert errors == []
    assert cleaned["LOG_STDOUT_LEVEL"] == "DEBUG"


def test_validate_config_log_stdout_level_case_insensitive():
    cleaned, errors = validate_config({"LOG_STDOUT_LEVEL": "debug"})
    assert errors == []
    assert cleaned["LOG_STDOUT_LEVEL"] == "DEBUG"


def test_validate_config_log_stdout_level_all_valid_values():
    for val in ("DEBUG", "INFO", "WARN", "ERROR"):
        cleaned, errors = validate_config({"LOG_STDOUT_LEVEL": val})
        assert errors == [], f"Expected no errors for {val!r}"
        assert cleaned["LOG_STDOUT_LEVEL"] == val


def test_validate_config_log_stdout_level_invalid_rejected():
    cleaned, errors = validate_config({"LOG_STDOUT_LEVEL": "VERBOSE"})
    assert len(errors) > 0
    assert "LOG_STDOUT_LEVEL" in errors[0]


# ---------------------------------------------------------------------------
# Dedup window tests (Run 10c)
# ---------------------------------------------------------------------------


def test_dedup_suppresses_duplicate_within_window():
    """Second identical msg within window is dropped; log_lines stays at 1 entry."""
    engine = _make_stub_engine(dedup_window=5)
    log(engine, "hello", "INFO")
    assert len(engine.log_lines) == 1
    log(engine, "hello", "INFO")  # duplicate within window
    assert len(engine.log_lines) == 1  # still 1 — suppressed
    assert engine._log_dedup_count == 1


def test_dedup_emits_suppressed_count_on_new_msg():
    """After duplicates are suppressed, a different msg flushes a synthetic entry."""
    engine = _make_stub_engine(dedup_window=5)
    log(engine, "hello", "INFO")
    log(engine, "hello", "INFO")  # suppressed
    log(engine, "hello", "INFO")  # suppressed
    assert engine._log_dedup_count == 2
    # Now send a different message — should emit synthetic + new entry
    log(engine, "world", "INFO")
    msgs = [e["msg"] for e in engine.log_lines]
    assert any("suppressed 2" in m for m in msgs), f"Expected suppressed entry in {msgs}"
    assert "world" in msgs[-1] or any("world" in m for m in msgs)


def test_dedup_different_level_is_not_duplicate():
    """Same msg but different level breaks the dedup chain."""
    engine = _make_stub_engine(dedup_window=5)
    log(engine, "hello", "INFO")
    log(engine, "hello", "WARN")  # different level — not a duplicate
    assert len(engine.log_lines) == 2


def test_dedup_disabled_when_window_is_zero():
    """LOG_DEDUP_WINDOW_SEC=0 disables dedup; all messages are emitted."""
    engine = _make_stub_engine(dedup_window=0)
    log(engine, "hello", "INFO")
    log(engine, "hello", "INFO")
    log(engine, "hello", "INFO")
    assert len(engine.log_lines) == 3


def test_dedup_state_reset_after_new_msg():
    """After emitting a new msg, dedup state resets to track the new msg."""
    engine = _make_stub_engine(dedup_window=5)
    log(engine, "hello", "INFO")
    log(engine, "world", "INFO")  # different msg — resets state
    assert engine._log_dedup_msg == "world"
    assert engine._log_dedup_count == 0


def test_validate_config_log_dedup_window_sec_valid():
    cleaned, errors = validate_config({"LOG_DEDUP_WINDOW_SEC": 10})
    assert errors == []
    assert cleaned["LOG_DEDUP_WINDOW_SEC"] == 10


def test_validate_config_log_dedup_window_sec_zero_allowed():
    cleaned, errors = validate_config({"LOG_DEDUP_WINDOW_SEC": 0})
    assert errors == []
    assert cleaned["LOG_DEDUP_WINDOW_SEC"] == 0


def test_validate_config_log_dedup_window_sec_out_of_range():
    cleaned, errors = validate_config({"LOG_DEDUP_WINDOW_SEC": 99})
    assert len(errors) > 0
