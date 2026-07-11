"""Unit tests for compute_tuner_bucket helper in tuner_app.tuning_engine.status."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import pytest

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.manager.tuner_manager import TunerManager
from tuner_app.tuning_engine.phases import (
    PHASE_BASELINE,
    PHASE_BRAIINS_DISCOVERY,
    PHASE_BRAIINS_PERPETUAL,
    PHASE_BRAIINS_WATTAGE_SEARCH,
    PHASE_DISCOVERY,
    PHASE_ERROR,
    PHASE_IDLE,
    PHASE_MEASURE,
    PHASE_OFFLINE,
    PHASE_PERPETUAL,
    PHASE_POLISH,
    PHASE_PROFILING,
    PHASE_SAVE,
    PHASE_SET_VOLTAGE,
    PHASE_STOPPED,
    PHASE_VF_EXPLORATION,
    PHASE_VOLTAGE_SWEEP,
)
from tuner_app.tuning_engine.status import compute_tuner_bucket


@pytest.mark.parametrize(
    "phase,engine_busy,expected",
    [
        # ALIVE-thread cases (engine_busy=True)
        (PHASE_IDLE, True, "idle"),
        (PHASE_ERROR, True, "error"),
        (PHASE_STOPPED, True, "stopping"),
        (PHASE_OFFLINE, True, "offline"),
        (PHASE_PERPETUAL, True, "maintaining"),
        (PHASE_DISCOVERY, True, "tuning"),
        (PHASE_SET_VOLTAGE, True, "tuning"),
        (PHASE_BASELINE, True, "tuning"),
        (PHASE_VF_EXPLORATION, True, "tuning"),
        (PHASE_PROFILING, True, "tuning"),
        (PHASE_POLISH, True, "tuning"),
        (PHASE_MEASURE, True, "tuning"),
        (PHASE_SAVE, True, "tuning"),
        (PHASE_BRAIINS_DISCOVERY, True, "tuning"),
        (PHASE_BRAIINS_WATTAGE_SEARCH, True, "tuning"),
        (PHASE_BRAIINS_PERPETUAL, True, "tuning"),
        (PHASE_VOLTAGE_SWEEP, True, "tuning"),
        ("phase_future_thing", True, "tuning"),
        ("", True, "tuning"),
        # DEAD-thread cases (engine_busy=False)
        (PHASE_IDLE, False, "idle"),
        (PHASE_ERROR, False, "error"),
        (PHASE_STOPPED, False, "stopped"),
        (PHASE_OFFLINE, False, "stopped"),
        (PHASE_PERPETUAL, False, "stopped"),
        (PHASE_DISCOVERY, False, "stopped"),
        (PHASE_SET_VOLTAGE, False, "stopped"),
        (PHASE_BASELINE, False, "stopped"),
        (PHASE_VF_EXPLORATION, False, "stopped"),
        (PHASE_PROFILING, False, "stopped"),
        (PHASE_POLISH, False, "stopped"),
        (PHASE_MEASURE, False, "stopped"),
        (PHASE_SAVE, False, "stopped"),
        (PHASE_BRAIINS_DISCOVERY, False, "stopped"),
        (PHASE_BRAIINS_WATTAGE_SEARCH, False, "stopped"),
        (PHASE_BRAIINS_PERPETUAL, False, "stopped"),
        (PHASE_VOLTAGE_SWEEP, False, "stopped"),
        ("phase_future_thing", False, "stopped"),
        ("", False, "stopped"),
    ],
)
def test_compute_tuner_bucket(phase: str, engine_busy: bool, expected: str) -> None:
    assert compute_tuner_bucket(phase, engine_busy) == expected


def test_get_status_source_references_tuner_bucket() -> None:
    """get_status must include 'tuner_bucket' as a returned dict key, derived
    via compute_tuner_bucket(engine.phase, engine_busy_flag)."""
    import inspect

    from tuner_app.tuning_engine.status import get_status

    src = inspect.getsource(get_status)
    assert '"tuner_bucket"' in src, (
        "get_status must include 'tuner_bucket' key in its returned dict"
    )
    assert "compute_tuner_bucket" in src, "get_status must call compute_tuner_bucket"


def test_compute_tuner_bucket_orphan_thread() -> None:
    """PHASE_PROFILING + engine_busy=False (orphan thread) returns 'stopped'."""
    assert compute_tuner_bucket(PHASE_PROFILING, engine_busy=False) == "stopped"


def test_compute_tuner_bucket_stopping() -> None:
    """PHASE_STOPPED + engine_busy=True (post-Stop wind-down) returns 'stopping'."""
    assert compute_tuner_bucket(PHASE_STOPPED, engine_busy=True) == "stopping"


def _make_engine_status_with_bucket(tuner_bucket="idle", firmware_type="epic"):
    return {
        "phase": "idle",
        "phase_detail": "",
        "tuned_stats": {},
        "firmware_type": firmware_type,
        "tuner_bucket": tuner_bucket,  # <-- the new field
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


def _make_mock_engine(tuner_bucket="idle", firmware_type="epic"):
    engine = MagicMock()
    engine.get_status.return_value = _make_engine_status_with_bucket(tuner_bucket, firmware_type)
    engine.last_summary = {}  # truthy so the lazy-refresh branch is skipped
    engine._get_profit_display_context.return_value = (0.10, None, 0.0)
    return engine


class TestOverviewTunerBucket(unittest.TestCase):
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

    def _manager_with_engines(self, ip_bucket_pairs):
        manager = TunerManager(state.CONFIG)
        ips = []
        for ip, bucket in ip_bucket_pairs:
            ips.append(ip)
            manager.engines[ip] = _make_mock_engine(bucket)
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ips
        return manager

    def test_state_counts_includes_stopping_when_fleet_empty(self):
        manager = TunerManager(state.CONFIG)
        # Empty fleet: no engines, no MINER_IPS
        overview = manager.get_overview()
        self.assertIn("stopping", overview["state_counts"])
        self.assertEqual(overview["state_counts"]["stopping"], 0)
        # Existing 6 buckets must still be present
        for key in ("idle", "tuning", "maintaining", "offline", "error", "stopped"):
            self.assertIn(key, overview["state_counts"])

    def test_overview_consumes_tuner_bucket_from_status(self):
        manager = self._manager_with_engines([("10.0.0.1", "stopping")])
        overview = manager.get_overview()
        row = next(m for m in overview["miners"] if m["ip"] == "10.0.0.1")
        self.assertEqual(row["tuner_bucket"], "stopping")
        self.assertEqual(overview["state_counts"]["stopping"], 1)

    def test_overview_orphan_thread_normalized_to_stopped(self):
        # Engine reports phase=profiling but bucket=stopped (from compute_tuner_bucket
        # in get_status with engine_busy=False). The overview must propagate the bucket,
        # NOT re-derive from phase alone.
        manager = self._manager_with_engines([("10.0.0.2", "stopped")])
        # Override the phase to PROFILING to simulate the orphan case
        engine = manager.engines["10.0.0.2"]
        status = engine.get_status.return_value
        status["phase"] = "phase3_profiling"
        status["tuner_bucket"] = "stopped"
        overview = manager.get_overview()
        row = next(m for m in overview["miners"] if m["ip"] == "10.0.0.2")
        self.assertEqual(row["tuner_bucket"], "stopped")
        self.assertGreaterEqual(overview["state_counts"]["stopped"], 1)
        self.assertEqual(overview["state_counts"]["tuning"], 0)
