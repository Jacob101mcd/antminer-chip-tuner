"""Tests for the timestamp-based retry-counter reset in ``TuningEngine._run``.

The historical reset compared ``run_duration > SUCCESSFUL_RUN_SEC`` —
which any slow failure mode tripped (e.g. a 10-min post-recovery
hashrate gate timeout, a 20-min Phase 1 voltage-settle timeout). Once
tripped, retries reset to 0 every iteration of an infinite recovery
loop and the engine never escalated to FATAL. The replacement gates
the reset on a wall-clock timestamp (``_iteration_confirmed_good_at``)
that ``_run_inner`` sets only AFTER recovering miner contact —
either ``_reset_to_safe_vf`` returning, or Phase 0's
``wait_for_mining_state`` returning. Iterations that never reach
either checkpoint leave the timestamp ``None`` and retries
accumulate properly.

These tests also lock in the removal of the post-recovery hashrate
gate from ``_run_inner``: an unstable tune cell can leave the miner
hashing well below stock, and gating on hashrate before
``_reset_to_safe_vf`` would falsely classify that as a recovery
failure and start the same infinite loop.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tuner_app.miner.exceptions import MinerNotReady


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
    engine._save_checkpoint = MagicMock()
    engine._mrr_sync = MagicMock()
    engine._enter_offline_mode = MagicMock()
    engine._wait_for_miner_online = MagicMock()
    engine._attempt_miner_recovery = MagicMock()
    return engine


class TestConfirmedGoodTimestampInit(unittest.TestCase):
    def test_starts_as_none(self):
        engine = _make_engine()
        self.assertIsNone(engine._iteration_confirmed_good_at)


class TestRetryCounterAccumulatesWhenNeverGood(unittest.TestCase):
    """When _run_inner fails before _reset_to_safe_vf or Phase 0 succeed
    (the only two points where _iteration_confirmed_good_at gets set),
    the timestamp stays None and the retry counter accumulates toward
    FATAL on every iteration. This is the regression fix: under the old
    duration-based check, retries reset every iteration because
    run_duration > SUCCESSFUL_RUN_SEC, and 5+ consecutive failures
    never escalated.
    """

    def test_fatal_after_max_retries_when_recovery_never_confirms_good(self):
        engine = _make_engine()

        call_count = {"n": 0}

        def fake_inner():
            # Simulate a long-failing iteration: takes > SUCCESSFUL_RUN_SEC
            # to fail (would have tripped the old reset), but never sets
            # _iteration_confirmed_good_at. With the new logic, retries
            # accumulate and FATAL fires after MAX_CONSECUTIVE_RETRIES.
            call_count["n"] += 1
            engine.phase = engine.PHASE_VF_EXPLORATION
            # The key invariant: confirmed_good stays None across iterations.
            self.assertIsNone(engine._iteration_confirmed_good_at)
            raise MinerNotReady("Miner failed to settle after 20 attempts (600s)")

        max_retries = engine.config["MAX_CONSECUTIVE_RETRIES"]
        # time.time() advances by 700s per call so run_duration ALWAYS
        # exceeds SUCCESSFUL_RUN_SEC=300 — the OLD check would have reset
        # retries every iteration.
        time_seq = iter(t * 700.0 for t in range(1, max_retries + 4))
        with (
            patch.object(engine, "_run_inner", side_effect=fake_inner),
            patch("tuner_app.tuning_engine.engine.time.time", side_effect=lambda: next(time_seq)),
            patch("time.sleep"),
        ):
            engine._run()

        self.assertEqual(engine.phase, engine.PHASE_ERROR)
        self.assertGreaterEqual(call_count["n"], max_retries + 1)

    def test_retries_reset_when_confirmed_good_more_than_successful_run_sec_ago(self):
        engine = _make_engine()

        # Pre-arrange: confirmed_good was set 600s ago (well over SUCCESSFUL_RUN_SEC=300).
        # The next failure should reset retries to 0 because we have evidence
        # the miner was healthy 10 min ago — this failure is fresh, not a loop.
        call_count = {"n": 0}

        def fake_inner():
            call_count["n"] += 1
            # First call: pretend we had a long good run.
            if call_count["n"] == 1:
                engine._iteration_confirmed_good_at = 1000.0
            if call_count["n"] >= 4:
                engine.running = False
            raise MinerNotReady("transient settle")

        # time sequence: each iteration's now() is 1700.0 (700s after
        # confirmed_good=1000.0). retries should reset because
        # (1700 - 1000) = 700 > SUCCESSFUL_RUN_SEC=300.
        with (
            patch.object(engine, "_run_inner", side_effect=fake_inner),
            patch("tuner_app.tuning_engine.engine.time.time", return_value=1700.0),
            patch("time.sleep"),
        ):
            engine._run()

        # Engine should NOT have hit FATAL — retries kept resetting.
        self.assertNotEqual(engine.phase, engine.PHASE_ERROR)


class TestHashrateGateRemoved(unittest.TestCase):
    def test_no_wait_for_hashrate_recovery_method(self):
        """The engine method is gone (the call site was the sole consumer)."""
        from tuner_app.tuning_engine.engine import TuningEngine

        self.assertFalse(hasattr(TuningEngine, "_wait_for_hashrate_recovery"))

    def test_no_wait_for_hashrate_recovery_function(self):
        """The recovery-module helper is gone too."""
        from tuner_app.tuning_engine import recovery

        self.assertFalse(hasattr(recovery, "wait_for_hashrate_recovery"))

    def test_run_inner_does_not_reference_hashrate_gate(self):
        """Audit grep — make sure no future refactor reintroduces the gate."""
        import inspect

        from tuner_app.tuning_engine.engine import TuningEngine

        src = inspect.getsource(TuningEngine._run_inner)
        self.assertNotIn("wait_for_hashrate_recovery", src)
        self.assertNotIn("Post-recovery hashrate gate", src)
        self.assertNotIn("Post-recovery gate", src)


if __name__ == "__main__":
    unittest.main()
