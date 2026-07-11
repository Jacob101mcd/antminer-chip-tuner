import unittest
from unittest.mock import MagicMock, patch

from tuner_app.config.defaults import CONFIG_DEFAULTS
from tuner_app.config.schema import CONFIG_BOUNDS
from tuner_app.tuning_engine.chip_tune_loop import phase3b_polish


def _make_engine(stabilize_wait, stabilize_wait_key="STABILITY_POLISH_STABILIZE_WAIT"):
    """Return a minimal mock engine for phase3b_polish tests.

    Uses a real dict as engine.config so dict.get() behaves identically to
    EffectiveConfig.get() for keys that are present (the common case under
    test). The mock has one board with one chip so the health-scoring loop
    exercises without touching per-chip arrays.
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

    cfg = {
        "STABILITY_POLISH_ROUNDS": 1,
        "STABILITY_POLISH_STEP_MHZ": 6.25,
        "CHIP_TUNE_DOWN_TOLERANCE": 15,
        "CHIP_FREQ_SPREAD_MHZ": 40,
        "VF_EXPLORE_F_MIN": 400,
        "STABILITY_POLISH_ROUND_SAMPLES": 1,
        "STABILITY_POLISH_ROUND_INTERVAL": 1,
        "STABILIZE_WAIT": 120,
    }
    cfg[stabilize_wait_key] = stabilize_wait
    engine.config = cfg

    # _apply_stable_freqs / _wait_for_mining_state / _save_checkpoint are no-ops.
    engine._apply_stable_freqs.return_value = None
    engine._wait_for_mining_state.return_value = None
    engine._save_checkpoint.return_value = None
    engine._update_live_data.return_value = None
    engine._detect_thermal_emergency.return_value = None

    # Phase constants referenced in the function body.
    engine.PHASE_POLISH = "polish"

    return engine


class TestPhase3bPolishStabilizeWait(unittest.TestCase):
    """Phase3b uses STABILITY_POLISH_STABILIZE_WAIT for its per-round wait."""

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_uses_stability_polish_stabilize_wait(self, mock_time, mock_collect):
        """phase3b_polish sleeps for STABILITY_POLISH_STABILIZE_WAIT seconds."""
        mock_collect.return_value = [[90.0]]  # one healthy chip → no drops
        engine = _make_engine(stabilize_wait=600)

        phase3b_polish(engine)

        slept = sum(c.args[0] for c in mock_time.sleep.call_args_list)
        self.assertGreaterEqual(slept, 590, "expected ~600s of sleep for stabilize wait")
        self.assertLessEqual(slept, 610)

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_falls_back_to_stabilize_wait_when_key_absent(self, mock_time, mock_collect):
        """phase3b_polish falls back to STABILIZE_WAIT when STABILITY_POLISH_STABILIZE_WAIT
        is absent from config (backward compat for old checkpoints)."""
        mock_collect.return_value = [[90.0]]
        engine = _make_engine(stabilize_wait=180, stabilize_wait_key="STABILIZE_WAIT")
        # Remove STABILITY_POLISH_STABILIZE_WAIT so the fallback is exercised.
        engine.config.pop("STABILITY_POLISH_STABILIZE_WAIT", None)

        phase3b_polish(engine)

        slept = sum(c.args[0] for c in mock_time.sleep.call_args_list)
        self.assertGreaterEqual(
            slept, 170, "expected ~180s of sleep falling back to STABILIZE_WAIT"
        )
        self.assertLessEqual(slept, 190)

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_startup_log_includes_stabilize_wait(self, mock_time, mock_collect):
        """The Phase 3b startup log must mention the configured stabilize wait so
        operators can verify their STABILITY_POLISH_STABILIZE_WAIT value took effect."""
        mock_collect.return_value = [[90.0]]
        engine = _make_engine(stabilize_wait=450)

        phase3b_polish(engine)

        logged = " ".join(str(c) for c in engine.log.call_args_list)
        self.assertIn("450", logged, "stabilize_wait value must appear in Phase 3b log")

    @patch("tuner_app.tuning_engine.chip_tune_loop.collect_chip_health_samples")
    @patch("tuner_app.tuning_engine.chip_tune_loop.time")
    def test_restart_round_log_names_correct_key(self, mock_time, mock_collect):
        """When the miner stops hashing mid-collection the restart log must
        reference STABILITY_POLISH_STABILIZE_WAIT, not STABILIZE_WAIT."""
        # Round 1: collect returns restart_round → restart log fires.
        # Round 2: _apply_stable_freqs sets running=False → outer loop exits.
        call_count = {"n": 0}

        def stop_on_second_apply(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                engine.running = False

        mock_collect.return_value = {"restart_round": True}
        engine = _make_engine(stabilize_wait=600)
        engine._apply_stable_freqs.side_effect = stop_on_second_apply

        phase3b_polish(engine)

        logged = " ".join(str(c) for c in engine.log.call_args_list)
        self.assertIn(
            "STABILITY_POLISH_STABILIZE_WAIT",
            logged,
            "restart_round log must reference STABILITY_POLISH_STABILIZE_WAIT",
        )
        self.assertNotIn(
            "full STABILIZE_WAIT",
            logged,
            "restart_round log must not say 'full STABILIZE_WAIT' (wrong key)",
        )


class TestPhase3bPolishMetadata(unittest.TestCase):
    def test_default_value_is_300(self):
        self.assertEqual(CONFIG_DEFAULTS["STABILITY_POLISH_STABILIZE_WAIT"], 300)

    def test_schema_bounds_correct(self):
        self.assertEqual(CONFIG_BOUNDS["STABILITY_POLISH_STABILIZE_WAIT"], (30, 31_536_000))
