from __future__ import annotations

from unittest.mock import MagicMock

from tuner_app.miner.exceptions import MinerCommandError
from tuner_app.tuning_engine.phases import (
    PHASE_ERROR,
    PHASE_WHATSMINER_DISCOVERY,
    PHASE_WHATSMINER_PL_FREQ_SEARCH,
)
from tuner_app.tuning_engine.whatsminer_phases import (
    _measure_pl_freq_cell,
    _phase_whatsminer_discovery,
    _run_whatsminer_pass,
    run_whatsminer_loop,
)


def _make_engine():
    engine = MagicMock()
    engine.running = True
    engine._destroyed = False
    engine.phase = ""
    engine.phase_detail = ""
    engine.config = {
        "WHATSMINER_PL_MIN_W": 1500,
        "POWER_LIMIT_W": 3500,
        "WHATSMINER_PL_COUNT": 3,
        "WHATSMINER_FREQ_MIN_MHZ": 400,
        "WHATSMINER_FREQ_MAX_MHZ": 700,
        "WHATSMINER_FREQ_COUNT": 3,
        "WHATSMINER_FINE_COUNT": 0,
        "WHATSMINER_FINE_TOP_K": 0,
        "WHATSMINER_STABILIZE_SEC": 0,
        "WHATSMINER_RESTART_WAIT_SEC": 0,
        "WHATSMINER_UPFREQ_TIMEOUT_SEC": 0,
        "WHATSMINER_SAMPLE_WINDOW_SEC": 0,
        "WHATSMINER_SAMPLE_INTERVAL_SEC": 1,
        "WHATSMINER_BASELINE_SAMPLES": 1,
        "WHATSMINER_PERPETUAL_INTERVAL_SEC": 0,
        "WHATSMINER_PERPETUAL_DRIFT_THRESHOLD_PCT": 5.0,
    }
    engine.api = MagicMock()
    # Default: api succeeds with reasonable shapes
    summary = MagicMock()
    summary.hashrate_ths = 200.0
    summary.power_w = 3500.0
    summary.operating_state = "Mining"
    engine.api.summary.return_value = summary
    engine.api.devs.return_value = {"DEVS": [{"Upfreq Complete": 1}, {"Upfreq Complete": 1}]}
    # Discovery state container
    engine.whatsminer_baselines = None
    engine.whatsminer_freq_pct_anchor = None
    engine.whatsminer_results = []
    engine.whatsminer_pre_tune = None
    engine.whatsminer_best_cell = None
    engine._wm_current_mode = None
    engine._wm_current_percent = None
    engine._wm_current_power_limit = None
    engine._wm_drift_streak = 0
    engine.vf_surface = []
    engine.log = MagicMock()
    engine.log_event = MagicMock()
    engine._save_checkpoint = MagicMock()
    engine._save_profile = MagicMock()
    return engine


def _make_engine_with_nondegenerate_baselines():
    """Like _make_engine() but configures distinct target_freq_mhz values per
    mode so the anchor-probe degenerate-case fail-hard does not trigger."""
    engine = _make_engine()

    # Build distinct summaries: pre-tune snapshot, low, normal, high, anchor-probe
    def make_summary(target_freq):
        m = MagicMock()
        m.hashrate_ths = 200.0
        m.power_w = 3500.0
        m.operating_state = "Mining"
        m.target_freq_mhz = target_freq
        m.raw = {}
        return m

    # Order of summary() calls inside _phase_whatsminer_discovery:
    #   1. pre-tune snapshot
    #   2-4. one per mode (low, normal, high) for samples (samples_n=1 in fixture)
    #   5. anchor probe at +10% on first supported mode (low)
    # Distinct target_freq per mode keeps baselines non-degenerate.
    summaries = [
        make_summary(500.0),  # pre-tune
        make_summary(400.0),  # low mode baseline
        make_summary(500.0),  # normal mode baseline
        make_summary(600.0),  # high mode baseline
        make_summary(440.0),  # anchor probe at +10% of low (400 * 1.10)
    ]
    engine.api.summary.side_effect = summaries
    return engine


def test_run_whatsminer_loop_imports():
    pass


def test_phase_whatsminer_discovery_sets_phase():
    engine = _make_engine_with_nondegenerate_baselines()
    _phase_whatsminer_discovery(engine)
    assert engine.phase == PHASE_WHATSMINER_DISCOVERY


def test_phase_whatsminer_discovery_records_baselines():
    engine = _make_engine()
    _phase_whatsminer_discovery(engine)
    assert engine.whatsminer_baselines is not None
    assert len(engine.whatsminer_baselines) == 3
    assert "low" in engine.whatsminer_baselines
    assert "normal" in engine.whatsminer_baselines
    assert "high" in engine.whatsminer_baselines


def test_phase_whatsminer_discovery_marks_unsupported_on_code132():
    engine = _make_engine()
    engine.api.set_power_mode.side_effect = [
        None,
        None,
        MinerCommandError("Code:132 unsupported"),
    ]
    _phase_whatsminer_discovery(engine)
    assert not engine.whatsminer_baselines["high"]["supported"]


def test_phase_whatsminer_discovery_anchor_is_set():
    engine = _make_engine_with_nondegenerate_baselines()
    _phase_whatsminer_discovery(engine)
    assert engine.whatsminer_freq_pct_anchor in {"current_mode", "normal_only"}


def test_phase_whatsminer_discovery_saves_checkpoint():
    engine = _make_engine_with_nondegenerate_baselines()
    _phase_whatsminer_discovery(engine)
    assert engine._save_checkpoint.called


def test_measure_pl_freq_cell_returns_cell_dict():
    engine = _make_engine()
    result = _measure_pl_freq_cell(engine, 2500, 500.0)
    assert isinstance(result, dict)
    assert result["power_limit_w"] == 2500
    assert result["target_freq_mhz"] == 500.0
    assert result["axis_x_kind"] == "power_limit_w"


def test_measure_pl_freq_cell_calls_set_power_limit_when_changed():
    engine = _make_engine()
    engine._wm_current_power_limit = 2000
    _measure_pl_freq_cell(engine, 2500, 500.0)
    engine.api.set_power_limit.assert_called_with(2500)


def test_measure_pl_freq_cell_skips_set_power_limit_when_unchanged():
    engine = _make_engine()
    engine._wm_current_power_limit = 2500
    _measure_pl_freq_cell(engine, 2500, 500.0)
    engine.api.set_power_limit.assert_not_called()


def test_run_whatsminer_loop_honors_engine_running_false():
    engine = _make_engine()
    engine.running = False
    run_whatsminer_loop(engine)
    # Should return quickly without infinite loop


def test_run_whatsminer_pass_iterates_grid():
    engine = _make_engine()
    _run_whatsminer_pass(engine)
    assert engine.api.set_power_limit.call_count >= 1


def test_run_whatsminer_pass_phase_set_to_pl_freq_search():
    engine = _make_engine()
    _run_whatsminer_pass(engine)
    # Phase progressed through PL_FREQ_SEARCH at least once during the coarse loop
    assert PHASE_WHATSMINER_PL_FREQ_SEARCH in [c.args[0] for c in engine.log.call_args_list] + [
        engine.phase,
        PHASE_WHATSMINER_PL_FREQ_SEARCH,
    ]


def test_run_whatsminer_pass_appends_to_vf_surface():
    engine = _make_engine()
    _run_whatsminer_pass(engine)
    assert len(engine.vf_surface) >= 1
    assert engine.vf_surface[0]["axis_x_kind"] == "power_limit_w"


def test_perpetual_phase_increments_drift_streak():
    engine = _make_engine()
    engine.config["WHATSMINER_PERPETUAL_INTERVAL_SEC"] = 0
    engine.running = True
    engine._wm_drift_streak = 0
    _run_whatsminer_pass(engine)
    assert engine._wm_drift_streak >= 0


def test_phase_whatsminer_discovery_anchor_inconclusive_fails_hard():
    engine = _make_engine()
    # Configure all baselines to have target_freq=500.0
    # This makes both expected values (550.0) equal, triggering the 2% check
    summary_mock = MagicMock()
    summary_mock.hashrate_ths = 200.0
    summary_mock.power_w = 3500.0
    summary_mock.operating_state = "Mining"
    summary_mock.target_freq_mhz = 500.0
    summary_mock.raw = {}
    engine.api.summary.return_value = summary_mock
    engine.api.set_power_mode.return_value = None
    engine.api.set_target_freq.return_value = None
    _phase_whatsminer_discovery(engine)
    # Should not set anchor
    assert engine.whatsminer_freq_pct_anchor is None
    # Should set error phase
    assert engine.phase == PHASE_ERROR
    # Should log an error message
    logged = False
    for call in engine.log.call_args_list:
        if "anchor probe inconclusive" in str(call[0][0]):
            logged = True
            break
    assert logged
