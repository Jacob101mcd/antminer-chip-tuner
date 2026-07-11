"""Tests for MRR_PUBLISH_DURING_POLISH knob and Phase 3b polish MRR sync.

Unit 3 of 4: verifies that MRR_PUBLISH_DURING_POLISH (fleet-ops, default False) causes
engine._mrr_sync('maintaining', reason='Entered Phase 3b polish') to fire exactly once
per chip-tune voltage entry into phase3b_polish, with correct dedup and lifecycle-clear
semantics.

TDD baseline — this file deliberately mixes two categories of test:

  Implementation-behavior tests (must FAIL until the feature lands):
    - TestMrrPublishDuringPolishConfig — knob not yet registered
    - TestMrrPolishAnnouncedFlag — structural: __init__ assignment not yet present
    - TestPhase3bPolishMrrSync.test_polish_calls_mrr_* — call not yet in phase3b_polish
    - TestPhase3bPolishMrrSync.test_polish_sets_announced_flag_after_call
    - TestPhase3bPolishMrrSync.test_polish_calls_mrr_even_when_sweep_hashrate_is_zero
    - TestPhase3bPolishMrrSync.test_polish_sync_fires_again_after_flag_reset
    - TestPolishAnnouncedClearsBetweenVoltages — clear not yet in chip_tune_orchestration
    - TestMrrPolishLifecycleClears — parity clears not yet in engine/lifecycle/phase_runners/retune

  Regression checks (must PASS regardless, guard existing behavior):
    - TestPhase3bPolishMrrSync.test_polish_does_not_call_mrr_when_knob_false
    - TestPhase3bPolishMrrSync.test_polish_dedup_within_single_invocation
    - TestPerpetualSyncStillFires — Phase 6 entry sync must still fire

The "knob_false → no call" and "dedup when already announced" tests pass vacuously now
(phase3b_polish never calls _mrr_sync at all), and that's intentional — they become
behavioral guards once the implementation lands.
"""

from __future__ import annotations

import pathlib
import re
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers — mirrors _make_engine in test_chip_tune_loop_polish.py
# ---------------------------------------------------------------------------


def _make_engine(
    knob_value: bool = False,
    sweep_hashrate: float = 200.0,
    polish_announced: bool = False,
) -> MagicMock:
    """Return a minimal mock engine for phase3b_polish tests.

    Uses a real dict as engine.config (same pattern as test_chip_tune_loop_polish.py).
    Adds MRR_PUBLISH_DURING_POLISH and _mrr_polish_announced so both the
    knob-false and knob-true paths can be exercised without touching real state.
    """
    engine = MagicMock()
    engine.running = True
    engine.polish_round = 0
    engine.polish_active = False
    engine.num_boards = 1
    engine.stable_freq_arrays = [[490.0]]
    engine.parked_chips = [set()]
    engine.baseline_scores = [[90.0]]
    engine.chips_converged = 0
    engine.chips_alive = 0
    engine.sweep_hashrate_ths = sweep_hashrate

    cfg: dict = {
        "STABILITY_POLISH_ROUNDS": 1,
        "STABILITY_POLISH_STEP_MHZ": 6.25,
        "STABILITY_POLISH_STABILIZE_WAIT": 1,  # 1s to keep tests fast
        "CHIP_TUNE_DOWN_TOLERANCE": 15,
        "CHIP_FREQ_SPREAD_MHZ": 40,
        "VF_EXPLORE_F_MIN": 400,
        "STABILITY_POLISH_ROUND_SAMPLES": 1,
        "STABILITY_POLISH_ROUND_INTERVAL": 1,
        "STABILIZE_WAIT": 1,
        "MRR_PUBLISH_DURING_POLISH": knob_value,
    }
    engine.config = cfg

    # Standard no-op delegates (same as polish test file).
    engine._apply_stable_freqs.return_value = None
    engine._wait_for_mining_state.return_value = None
    engine._save_checkpoint.return_value = None
    engine._update_live_data.return_value = None
    engine._detect_thermal_emergency.return_value = None

    # Phase constant used inside phase3b_polish.
    engine.PHASE_POLISH = "polish"

    # The flag being introduced by this unit.
    engine._mrr_polish_announced = polish_announced

    return engine


# ---------------------------------------------------------------------------
# 1. Config registration checks
# ---------------------------------------------------------------------------


class TestMrrPublishDuringPolishConfig(unittest.TestCase):
    """MRR_PUBLISH_DURING_POLISH is registered in FLEET_OPS_KEYS with default False."""

    def test_knob_in_fleet_ops_keys(self):
        """MRR_PUBLISH_DURING_POLISH must appear in FLEET_OPS_KEYS."""
        from tuner_app.constants import FLEET_OPS_KEYS

        self.assertIn("MRR_PUBLISH_DURING_POLISH", FLEET_OPS_KEYS)

    def test_knob_default_false_in_fleet_ops_defaults(self):
        """CONFIG_DEFAULTS must contain MRR_PUBLISH_DURING_POLISH=False (fleet-ops key)."""
        from tuner_app.config.defaults import CONFIG_DEFAULTS

        self.assertIn("MRR_PUBLISH_DURING_POLISH", CONFIG_DEFAULTS)
        self.assertIs(CONFIG_DEFAULTS["MRR_PUBLISH_DURING_POLISH"], False)


# ---------------------------------------------------------------------------
# 2. Engine init exposes the flag
# ---------------------------------------------------------------------------


class TestMrrPolishAnnouncedFlag(unittest.TestCase):
    """engine.py __init__ must initialize _mrr_polish_announced = False."""

    def test_engine_init_clears_polish_announced(self):
        """engine.py __init__ must contain an uncommented assignment line
        ``self._mrr_polish_announced = False``.

        Structural test: reads the source file directly and uses a regex to
        match an actual assignment line (not a comment). Mirrors the pattern
        used in TestMrrPolishLifecycleClears for parity checks.

        Pattern: ``r'^\\s+self\\._mrr_polish_announced\\s*=\\s*False\\s*(?:#.*)?$'``
        matches an indented assignment with an optional inline comment but NOT
        a commented-out line (which would start with ``#`` before the word ``self``).
        """
        src = (
            pathlib.Path(__file__).parent.parent.parent
            / "tuner_app"
            / "tuning_engine"
            / "engine.py"
        )
        content = src.read_text(encoding="utf-8")
        pattern = re.compile(
            r"^\s+self\._mrr_polish_announced\s*=\s*False\s*(?:#.*)?$",
            re.MULTILINE,
        )
        matches = pattern.findall(content)
        self.assertGreater(
            len(matches),
            0,
            "engine.py __init__ must contain an uncommented "
            "'self._mrr_polish_announced = False' assignment line",
        )


# ---------------------------------------------------------------------------
# 3. Phase 3b MRR sync behaviour
# ---------------------------------------------------------------------------


class TestPhase3bPolishMrrSync(unittest.TestCase):
    """Core behaviour: phase3b_polish fires (or withholds) _mrr_sync correctly."""

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_polish_does_not_call_mrr_when_knob_false(self, _mock_time, mock_collect):
        """phase3b_polish must NOT call _mrr_sync when MRR_PUBLISH_DURING_POLISH is False."""
        from tuner_app.tuning_engine.chip_tune_loop import phase3b_polish

        mock_collect.return_value = [[90.0]]
        engine = _make_engine(knob_value=False)

        phase3b_polish(engine)

        engine._mrr_sync.assert_not_called()

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_polish_calls_mrr_when_knob_true_and_hashrate_available(self, _mock_time, mock_collect):
        """phase3b_polish calls _mrr_sync('maintaining', 'Entered Phase 3b polish') once."""
        from tuner_app.tuning_engine.chip_tune_loop import phase3b_polish

        mock_collect.return_value = [[90.0]]
        engine = _make_engine(knob_value=True, sweep_hashrate=200.0, polish_announced=False)

        phase3b_polish(engine)

        # Exactly one call to _mrr_sync with the expected positional + keyword args.
        engine._mrr_sync.assert_called_once_with("maintaining", reason="Entered Phase 3b polish")

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_polish_dedup_within_single_invocation(self, _mock_time, mock_collect):
        """phase3b_polish must NOT call _mrr_sync when _mrr_polish_announced is already True."""
        from tuner_app.tuning_engine.chip_tune_loop import phase3b_polish

        mock_collect.return_value = [[90.0]]
        # Simulate: flag already set (second iteration of the same polish phase).
        engine = _make_engine(knob_value=True, sweep_hashrate=200.0, polish_announced=True)

        phase3b_polish(engine)

        engine._mrr_sync.assert_not_called()

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_polish_sets_announced_flag_after_call(self, _mock_time, mock_collect):
        """After phase3b_polish fires the sync, engine._mrr_polish_announced must be True."""
        from tuner_app.tuning_engine.chip_tune_loop import phase3b_polish

        mock_collect.return_value = [[90.0]]
        engine = _make_engine(knob_value=True, sweep_hashrate=200.0, polish_announced=False)

        phase3b_polish(engine)

        self.assertTrue(engine._mrr_polish_announced)

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_polish_calls_mrr_even_when_sweep_hashrate_is_zero(self, _mock_time, mock_collect):
        """phase3b_polish calls _mrr_sync even when sweep_hashrate_ths == 0.

        The internal skip ("sweep_hashrate=0" path in mrr_sync.py:105-115) happens
        deeper inside _mrr_sync, NOT in phase3b_polish itself. phase3b_polish must
        NOT gate the call on hashrate availability — doing so would silently suppress
        the sync when cold-starting into Phase 3b.
        """
        from tuner_app.tuning_engine.chip_tune_loop import phase3b_polish

        mock_collect.return_value = [[90.0]]
        engine = _make_engine(knob_value=True, sweep_hashrate=0.0, polish_announced=False)

        phase3b_polish(engine)

        engine._mrr_sync.assert_called_once_with("maintaining", reason="Entered Phase 3b polish")

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_polish_sync_fires_again_after_flag_reset(self, _mock_time, mock_collect):
        """_mrr_sync fires on every voltage where _mrr_polish_announced is cleared.

        Simulates chip_tune_orchestration resetting the flag at the next voltage's
        chip-tune entry. After the reset, a second phase3b_polish call must fire
        _mrr_sync again — total call count == 2.
        """
        from tuner_app.tuning_engine.chip_tune_loop import phase3b_polish

        mock_collect.return_value = [[90.0]]
        engine = _make_engine(knob_value=True, sweep_hashrate=200.0, polish_announced=False)

        # First voltage: flag is False AND polish_round == 0 → sync fires, flag set to True.
        phase3b_polish(engine)

        # Simulate chip_tune_orchestration starting at a fresh voltage:
        # polish_round = 0 AND _mrr_polish_announced = False (both reset together
        # at chip_tune_orchestration.py:319-325 alongside polish_active = False).
        engine._mrr_polish_announced = False
        engine.polish_round = 0

        # Second voltage: flag cleared AND polish_round == 0 → sync fires again.
        phase3b_polish(engine)

        self.assertEqual(
            engine._mrr_sync.call_count,
            2,
            "Expected _mrr_sync called twice (once per voltage after flag reset); "
            f"got {engine._mrr_sync.call_count} call(s)",
        )


# ---------------------------------------------------------------------------
# 4. Flag cleared per voltage (structural assertion)
# ---------------------------------------------------------------------------


class TestPolishAnnouncedClearsBetweenVoltages(unittest.TestCase):
    """chip_tune_orchestration.py must clear _mrr_polish_announced where polish_round resets."""

    def test_chip_tune_orchestration_clears_polish_flag_at_voltage_start(self):
        """Structural test: engine._mrr_polish_announced = False appears in the same
        function body as engine.polish_round = 0 in chip_tune_orchestration.py.

        Exercising run_phase3_phase4_at_voltage end-to-end in a unit test is
        impractical (requires live miner infrastructure), so this test reads the source
        file directly to assert the clear is present alongside the polish_round reset.

        Uses a regex to match an uncommented assignment line — a commented-out line
        ``# engine._mrr_polish_announced = False`` would NOT pass this test.
        """
        src = (
            pathlib.Path(__file__).parent.parent.parent
            / "tuner_app"
            / "tuning_engine"
            / "chip_tune_orchestration.py"
        )
        content = src.read_text(encoding="utf-8")

        polish_pattern = re.compile(
            r"^\s+engine\._mrr_polish_announced\s*=\s*False\s*(?:#.*)?$",
            re.MULTILINE,
        )
        polish_matches = polish_pattern.findall(content)
        self.assertGreater(
            len(polish_matches),
            0,
            "chip_tune_orchestration.py must contain an uncommented "
            "'engine._mrr_polish_announced = False' assignment line",
        )

        # Sanity check: polish_round reset must still be present (baseline invariant).
        polish_round_pattern = re.compile(
            r"^\s+engine\.polish_round\s*=\s*0\s*(?:#.*)?$",
            re.MULTILINE,
        )
        self.assertGreater(
            len(polish_round_pattern.findall(content)),
            0,
            "chip_tune_orchestration.py must reset polish_round at voltage start (sanity check)",
        )


# ---------------------------------------------------------------------------
# 5. Lifecycle-level clears (structural — mirrors count of phase6 clears)
# ---------------------------------------------------------------------------


class TestMrrPolishLifecycleClears(unittest.TestCase):
    """_mrr_polish_announced = False must appear everywhere _mrr_phase6_announced = False does.

    Uses regex matching (not substring count) so commented-out lines don't
    satisfy the assertion.  Pattern matches indented assignment lines with an
    optional inline comment but NOT lines whose first non-space character is ``#``.
    """

    _BASE = pathlib.Path(__file__).parent.parent.parent / "tuner_app" / "tuning_engine"

    _PHASE6_PATTERN = re.compile(
        r"^\s+(?:self|engine)\._mrr_phase6_announced\s*=\s*False\s*(?:#.*)?$",
        re.MULTILINE,
    )
    _POLISH_PATTERN = re.compile(
        r"^\s+(?:self|engine)\._mrr_polish_announced\s*=\s*False\s*(?:#.*)?$",
        re.MULTILINE,
    )

    def _count_phase6(self, filepath: pathlib.Path) -> int:
        return len(self._PHASE6_PATTERN.findall(filepath.read_text(encoding="utf-8")))

    def _count_polish(self, filepath: pathlib.Path) -> int:
        return len(self._POLISH_PATTERN.findall(filepath.read_text(encoding="utf-8")))

    def test_engine_py_clears_match(self):
        """engine.py must have exactly as many uncommented _mrr_polish_announced = False
        lines as uncommented _mrr_phase6_announced = False lines."""
        path = self._BASE / "engine.py"
        phase6_count = self._count_phase6(path)
        polish_count = self._count_polish(path)
        self.assertEqual(
            polish_count,
            phase6_count,
            f"engine.py: _mrr_polish_announced = False ({polish_count}) != "
            f"_mrr_phase6_announced = False ({phase6_count})",
        )

    def test_lifecycle_py_clears_match(self):
        """lifecycle.py must have exactly as many uncommented _mrr_polish_announced = False
        lines as uncommented _mrr_phase6_announced = False lines."""
        path = self._BASE / "lifecycle.py"
        phase6_count = self._count_phase6(path)
        polish_count = self._count_polish(path)
        self.assertEqual(
            polish_count,
            phase6_count,
            f"lifecycle.py: _mrr_polish_announced = False ({polish_count}) != "
            f"_mrr_phase6_announced = False ({phase6_count})",
        )

    def test_phase_runners_py_clears_match(self):
        """phase_runners.py must have exactly as many uncommented _mrr_polish_announced = False
        lines as uncommented _mrr_phase6_announced = False lines."""
        path = self._BASE / "phase_runners.py"
        phase6_count = self._count_phase6(path)
        polish_count = self._count_polish(path)
        self.assertEqual(
            polish_count,
            phase6_count,
            f"phase_runners.py: _mrr_polish_announced = False ({polish_count}) != "
            f"_mrr_phase6_announced = False ({phase6_count})",
        )

    def test_retune_py_clears_match(self):
        """retune.py must have exactly as many uncommented _mrr_polish_announced = False
        lines as uncommented _mrr_phase6_announced = False lines."""
        path = self._BASE / "retune.py"
        phase6_count = self._count_phase6(path)
        polish_count = self._count_polish(path)
        self.assertEqual(
            polish_count,
            phase6_count,
            f"retune.py: _mrr_polish_announced = False ({polish_count}) != "
            f"_mrr_phase6_announced = False ({phase6_count})",
        )


# ---------------------------------------------------------------------------
# 6. Perpetual (Phase 6) entry sync is NOT regressed
# ---------------------------------------------------------------------------


class TestPerpetualSyncStillFires(unittest.TestCase):
    """Existing Phase 6 entry sync must still fire on monitor cycle entry."""

    def test_perpetual_entry_still_calls_mrr_sync(self):
        """do_monitor_cycle_body fires _mrr_sync('maintaining', ...) on first Phase 6 entry.

        This is a regression guard: the new polish-sync path must not disturb
        the existing Phase 6 entry sync at monitor.py:90-113. We stop the engine
        after the MRR sync fires (by setting running=False as a side effect of
        the _mrr_phase6_announced assignment) so the test doesn't enter the
        long check-interval sleep loop.
        """
        from tuner_app.tuning_engine.monitor import do_monitor_cycle_body

        engine = MagicMock()
        engine._mrr_phase6_announced = False
        engine.sweep_voltage_mv = 14000
        engine.sweep_hashrate_ths = 200.0
        engine.voltage_adjustment_mv = 0
        engine.active_sweep_voltage_mv = 14000
        engine.voltage_results = []
        engine.running = True
        engine.phase = "perpetual"
        engine.PHASE_PERPETUAL = "perpetual"
        engine.api.supports_per_chip_tuning.return_value = False

        # _refresh_sweep_reference sets sweep_voltage_mv on the engine object
        # inside do_monitor_cycle_body — keep the mock's value stable.
        engine._refresh_sweep_reference.return_value = None
        engine._apply_stable_freqs.return_value = None
        engine._wait_for_mining_state.return_value = None
        engine._update_live_data.return_value = None

        # Provide real numeric config values so monitor.py arithmetic doesn't
        # TypeError on MagicMock comparisons inside the sleep loop.
        engine.config = {
            "PERPETUAL_VOLTAGE_CHECK_MIN": 10,
            "PERPETUAL_HASHRATE_DEADBAND_PCT": 0.5,
            "PERPETUAL_VOLTAGE_STEP_MV": 50,
            "PERPETUAL_VOLTAGE_MAX_DELTA_MV": 300,
            "PERPETUAL_RESTART_MIN_HOURS": 24,
        }

        # After _mrr_sync fires and _mrr_phase6_announced is set to True,
        # stop the engine so we don't block in the check-interval sleep loop.
        _sync_calls = []

        def _mrr_sync_side_effect(intent, **kwargs):
            _sync_calls.append((intent, kwargs))
            # Stop the engine so the monitor loop exits promptly.
            engine.running = False

        engine._mrr_sync.side_effect = _mrr_sync_side_effect

        # re_rank_active_voltage is called first; let it be a no-op.
        with (
            patch("tuner_app.tuning_engine.monitor.re_rank_active_voltage", return_value=None),
            patch("tuner_app.tuning_engine.monitor.time"),
        ):
            do_monitor_cycle_body(engine)

        # Must have called _mrr_sync with 'maintaining' as first arg and a
        # reason that references the Phase 6 / monitor entry event.
        maintaining_calls = [c for c in _sync_calls if c[0] == "maintaining"]
        self.assertTrue(
            len(maintaining_calls) >= 1,
            f"Expected at least one _mrr_sync('maintaining', ...) call; got: {_sync_calls}",
        )
        # Verify the reason is the pre-existing "Entered monitor" string from
        # monitor.py:112 — NOT the new polish reason (regression guard).
        # The exact string is verified by reading monitor.py before writing this test.
        reasons = [c[1].get("reason", "") for c in maintaining_calls]
        self.assertTrue(
            any("Entered monitor" in r for r in reasons),
            f"Expected reason containing 'Entered monitor' (monitor.py:112); reasons={reasons}",
        )
