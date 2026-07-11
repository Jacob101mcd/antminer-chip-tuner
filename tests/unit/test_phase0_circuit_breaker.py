"""Tests for the Phase-0-specific circuit breaker in ``TuningEngine._run``.

When a miner repeatedly refuses connections during Phase 0's command
burst (e.g. LuxOS firmware overload tripping port 4028 into refusing
new TCP connections), the engine used to cycle forever:
``wait_for_miner_online`` resets ``offline_hits`` after a successful
reconnect, then Phase 0 storms the miner again, refusal returns,
loop repeats. The circuit breaker tracks consecutive Phase-0
``MinerOfflineError`` raises in a separate counter and escalates to
``PHASE_ERROR`` after ``PHASE0_CIRCUIT_BREAKER_THRESHOLD`` hits.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tuner_app.miner.exceptions import MinerOfflineError


class _FakeConfig:
    def __init__(self, overrides=None):
        self._data = {
            "API_PORT": 4028,
            "PASSWORD": "letmein",
            "firmware_type": "epic",
            "OFFLINE_FAILURE_THRESHOLD": 3,
            "OFFLINE_POLL_INTERVAL": 30,
            "MAX_CONSECUTIVE_RETRIES": 5,
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
    """Construct a TuningEngine with disk I/O suppressed and api stubbed."""
    from tuner_app.tuning_engine.engine import TuningEngine

    cfg = _FakeConfig()
    with (
        patch("tuner_app.tuning_engine.engine.persistence.restore_saved_state"),
        patch("tuner_app.tuning_engine.engine.logging_.load_log_from_disk"),
    ):
        engine = TuningEngine("192.0.2.99", cfg)
    engine.running = True
    engine.api = MagicMock()
    # Silence log persistence — we don't care about the JSONL file in unit tests.
    engine.log = MagicMock()
    engine._save_checkpoint = MagicMock()
    engine._mrr_sync = MagicMock()
    engine._enter_offline_mode = MagicMock()
    engine._wait_for_miner_online = MagicMock()
    engine._attempt_miner_recovery = MagicMock()
    return engine


class TestPhase0CircuitBreaker(unittest.TestCase):
    def test_initial_counter_is_zero(self):
        engine = _make_engine()
        self.assertEqual(engine._phase0_consecutive_offline_hits, 0)

    def test_threshold_constant_present(self):
        # The constant must be a positive int — the breaker depends on it.
        from tuner_app.tuning_engine.engine import TuningEngine

        self.assertIsInstance(TuningEngine.PHASE0_CIRCUIT_BREAKER_THRESHOLD, int)
        self.assertGreaterEqual(TuningEngine.PHASE0_CIRCUIT_BREAKER_THRESHOLD, 1)

    def test_offline_in_phase_discovery_increments_counter(self):
        engine = _make_engine()
        # Simulate _run_inner raising MinerOfflineError while still in
        # Phase 0 (the engine sets phase = PHASE_DISCOVERY at the top of
        # phase0_discovery, before any work).
        call_count = {"n": 0}

        def fake_inner():
            call_count["n"] += 1
            engine.phase = engine.PHASE_DISCOVERY
            if call_count["n"] >= engine.PHASE0_CIRCUIT_BREAKER_THRESHOLD:
                # Once we reach the threshold the breaker should fire
                # before our next iteration. Stop the loop so the test
                # doesn't depend on the breaker shutting down via return.
                engine.running = False
            raise MinerOfflineError("WinError 10061")

        with patch.object(engine, "_run_inner", side_effect=fake_inner), patch("time.sleep"):
            engine._run()
        # The breaker should have flipped engine to PHASE_ERROR.
        self.assertEqual(engine.phase, engine.PHASE_ERROR)
        self.assertGreaterEqual(
            engine._phase0_consecutive_offline_hits,
            engine.PHASE0_CIRCUIT_BREAKER_THRESHOLD,
        )

    def test_offline_outside_phase_discovery_does_not_increment(self):
        engine = _make_engine()
        # Phase != DISCOVERY when the offline error fires — counter must NOT
        # increment. We simulate one offline event at PHASE_PERPETUAL.
        call_count = {"n": 0}

        def fake_inner():
            call_count["n"] += 1
            engine.phase = engine.PHASE_PERPETUAL
            if call_count["n"] >= 2:
                engine.running = False
            raise MinerOfflineError("transient")

        with patch.object(engine, "_run_inner", side_effect=fake_inner), patch("time.sleep"):
            engine._run()
        self.assertEqual(engine._phase0_consecutive_offline_hits, 0)

    def test_breaker_logs_and_calls_mrr_sync_on_trip(self):
        engine = _make_engine()
        engine._phase0_consecutive_offline_hits = engine.PHASE0_CIRCUIT_BREAKER_THRESHOLD - 1

        def fake_inner():
            engine.phase = engine.PHASE_DISCOVERY
            raise MinerOfflineError("port refused")

        with patch.object(engine, "_run_inner", side_effect=fake_inner), patch("time.sleep"):
            engine._run()
        self.assertEqual(engine.phase, engine.PHASE_ERROR)
        self.assertIn("Phase 0", engine.phase_detail)
        engine._mrr_sync.assert_called()
        self.assertEqual(engine._mrr_sync.call_args.args[0], "error")

    def test_phase0_discovery_resets_counter_after_set_perpetualtune(self):
        # Direct unit check on the reset path: phase0_discovery is supposed
        # to clear the counter once it reaches set_perpetualtune. Run the
        # function with an engine where the counter is non-zero and verify
        # it ends at zero. We patch the api methods so the function flows
        # through without side effects; mrr functions are also no-ops here.
        from tuner_app.miner.types import HardwareTopology, MinerSummary
        from tuner_app.tuning_engine import phase_runners

        engine = _make_engine()
        engine._phase0_consecutive_offline_hits = 2
        engine.config_snapshot = {}

        # Stub everything phase0_discovery calls so it can run end-to-end.
        engine._mrr_apply_pool_config = MagicMock()
        engine._mrr_sync = MagicMock()
        engine._capture_live_stock_baseline = MagicMock()
        engine._resize_board_arrays = MagicMock()
        engine._wait_for_mining_state = MagicMock()
        # Avoid touching the real CONFIG dict for snapshot iteration —
        # iter_all_config_keys is bound by-name in phase_runners, so we patch
        # the imported reference in that module, not the source module.
        with patch.object(phase_runners, "iter_all_config_keys", return_value=()):
            mining_summary = MinerSummary(
                operating_state="Mining",
                hashrate_ths=200.0,
                power_w=0.0,
                fan_speed=0,
            )
            engine.api.summary = MagicMock(return_value=mining_summary)
            engine.api.summary_lite = MagicMock(return_value=mining_summary)
            engine.api.hardware_topology = MagicMock(
                return_value=HardwareTopology(
                    num_boards=3,
                    chips_per_board=108,
                    psu_min_mv=11877,
                    psu_max_mv=15182,
                )
            )
            engine.api.set_perpetualtune = MagicMock()
            engine.api.start_mining = MagicMock()
            phase_runners.phase0_discovery(engine)
        self.assertEqual(engine._phase0_consecutive_offline_hits, 0)


if __name__ == "__main__":
    unittest.main()
