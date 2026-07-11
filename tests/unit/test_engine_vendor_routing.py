"""Tests for vendor-aware Phase 0/1/2/3/6 routing (Run 5).

Covers each Bixbit branch introduced in Run 5:
- Phase 0: Bixbit sets num_boards=3, chips_per_board=0; skips capabilities()
- Phase 0: topology invalidation guard skips when chips_per_board==0
- Phase 1: Bixbit calls set_power_limit after V+F settle
- Phase 2: Bixbit populates empty per-chip baseline arrays
- Phase 3: run_phase3_phase4_at_voltage early-returns on Bixbit with
           chip_tune_active=False; never calls _phase3_profiling
- Phase 6: do_monitor_cycle_body skips evaluate_chip_tune_fallback on Bixbit
- reset: park_dead_chips_from_baseline short-circuits when chips_per_board==0
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_mock_api(vendor: str) -> MagicMock:
    """Return a MagicMock API object pre-configured with firmware_type() and capability methods."""
    from tuner_app.miner.types import HardwareTopology

    api = MagicMock()
    api.firmware_type.return_value = vendor
    api.tuning_strategy.return_value = {
        "braiins": "wattage_search",
        "whatsminer": "power_limit_freq_search",
    }.get(vendor, "voltage_chip_tune")
    # Stub capability methods so phase_runners.py capability-flag predicates work
    api.supports_per_chip_tuning.return_value = vendor not in ("bixbit", "braiins", "luxos")
    api.has_external_power_limit.return_value = vendor in ("bixbit", "braiins")
    api.has_capabilities_endpoint.return_value = vendor in ("epic", "luxos")
    api.has_internal_perpetual_tune.return_value = vendor in ("braiins",)
    # Stub hardware_topology() so topology reads don't return MagicMock objects
    chips_per_board = 0 if vendor in ("bixbit", "braiins") else 108
    api.hardware_topology.return_value = HardwareTopology(
        num_boards=3,
        chips_per_board=chips_per_board,
        psu_min_mv=11877,
        psu_max_mv=15182,
        # Routing tests isolate vendor branching. Adapter provenance behavior
        # is covered in test_hardware_topology and test_voltage_bounds_safety.
        psu_bounds_verified=True,
        psu_bounds_source="test:verified-live-bounds",
    )
    return api


def _make_engine(vendor: str = "epic", num_boards: int = 3, chips_per_board: int = 108):
    """Return a MagicMock engine with minimal attributes needed by the tested functions."""
    engine = MagicMock()
    engine.api = _make_mock_api(vendor)
    engine.num_boards = num_boards
    engine.chips_per_board = chips_per_board
    engine.running = True
    engine.log = MagicMock()
    engine.phase = None
    engine.phase_detail = None
    engine.config = {
        "START_VOLTAGE_MV": 0,
        "CHIP_FREQ_SPREAD_MHZ": 25,
        "STABILIZE_WAIT": 1,
        "DEAD_CHIP_SCORE": 20,
        "DEAD_CHIP_FREQ": 50,
        "POWER_LIMIT_W": 3500,
        "SETTLE_MAX_ATTEMPTS": 2,
        "SETTLE_POLL_INTERVAL": 1,
        "SETTLE_VOLTAGE_TOLERANCE_MV": 200,
        "EFFICIENCY_MEASURE_WAIT": 1,
        "PERPETUAL_VOLTAGE_CHECK_MIN": 1,
        "PERPETUAL_HASHRATE_DEADBAND_PCT": 2.0,
        "PERPETUAL_VOLTAGE_STEP_MV": 50,
        "FREQ_SEARCH_TOLERANCE_MHZ": 7,
        "BASELINE_SAMPLES": 1,
        "BASELINE_INTERVAL": 1,
    }
    engine.config_snapshot = {}
    engine.psu_max_mv = 15182
    engine.psu_min_mv = 11877
    engine.voltage_topology = engine.api.hardware_topology.return_value
    engine.start_voltage_mv = 11877
    engine.min_voltage_mv = 11877
    engine.baseline_scores = [[] for _ in range(num_boards)]
    engine.baseline_chip_temps = [[] for _ in range(num_boards)]
    engine.baseline_chip_hashrates = [[] for _ in range(num_boards)]
    engine.baseline_freq_arrays = [[] for _ in range(num_boards)]
    engine.stable_freq_arrays = [[] for _ in range(num_boards)]
    engine.proposed_freqs = [[] for _ in range(num_boards)]
    engine.sweep_freq_arrays = [[] for _ in range(num_boards)]
    engine.voltage_results = []
    engine.vf_surface = []
    engine.best_efficiency = None
    engine.current_step_started_at = None
    engine.current_sweep_voltage_mv = None
    engine.profiling_completion_pct = 0.0
    engine.chips_stable_pct = 0.0
    engine.chips_converged = 0
    engine.chips_alive = 0
    engine.PHASE_DISCOVERY = "discovery"
    engine.PHASE_BASELINE = "baseline"
    engine.PHASE_SET_VOLTAGE = "set_voltage"
    engine.PHASE_MEASURE = "measure"
    engine.PHASE_PERPETUAL = "perpetual"
    engine._mrr_phase6_announced = True  # skip MRR block in most tests
    engine.sweep_voltage_mv = 14000
    engine.sweep_hashrate_ths = 200.0
    engine.voltage_adjustment_mv = 0
    engine.active_sweep_voltage_mv = None
    return engine


def _make_minimal_summary():
    """Return a MinerSummary with zero hashrate/power so summary() mock resolves cleanly."""
    from tuner_app.miner.types import MinerSummary

    return MinerSummary(
        operating_state="Mining",
        hashrate_ths=0.0,
        power_w=0.0,
        fan_speed=0,
    )


# ---------------------------------------------------------------------------
# Test 1: Phase 0 on Bixbit sets num_boards=3, chips_per_board=0 and does
#         NOT call engine.api.capabilities()
# ---------------------------------------------------------------------------


class TestPhase0BixbitSkipsCaps(unittest.TestCase):
    _TEST_IP = "bixbit-phase0-test-ip"

    def setUp(self):
        # Populate real v3 state.CONFIG so iter_all_config_keys() works without
        # patching.  Register a per-miner config so EffectiveConfig resolves.
        from tuner_app import state
        from tuner_app.config.defaults import apply_defaults

        apply_defaults()
        state.MINER_CONFIGS[self._TEST_IP] = {"firmware_type": "bixbit"}

    def tearDown(self):
        from tuner_app import state

        state.MINER_CONFIGS.pop(self._TEST_IP, None)

    def test_bixbit_phase0_sets_sentinel_and_skips_caps(self):
        """Phase 0 Bixbit: num_boards=3, chips_per_board=0, no capabilities() call."""
        from tuner_app.config.effective import EffectiveConfig
        from tuner_app.tuning_engine.phase_runners import phase0_discovery

        engine = _make_engine("bixbit", num_boards=3, chips_per_board=108)
        # Use a real EffectiveConfig so iter_all_config_keys() snapshot succeeds.
        engine.config = EffectiveConfig(self._TEST_IP)

        # summary() returns a MinerSummary DTO so the check passes
        engine.api.summary.return_value = _make_minimal_summary()
        engine._capture_live_stock_baseline = MagicMock()
        engine._resize_board_arrays = MagicMock()
        engine._wait_for_mining_state = MagicMock()
        engine._mrr_phase6_announced = False
        engine._mrr_apply_pool_config = MagicMock()
        engine._mrr_sync = MagicMock()

        phase0_discovery(engine)

        # Must NOT call capabilities()
        engine.api.capabilities.assert_not_called()

        # Sentinel values
        self.assertEqual(engine.num_boards, 3)
        self.assertEqual(engine.chips_per_board, 0)
        self.assertEqual(engine.psu_max_mv, 15182)


# ---------------------------------------------------------------------------
# Test 2: Phase 0 topology invalidation guard skips when chips_per_board==0
#         (Bixbit init should NOT trigger a stale-state wipe)
# ---------------------------------------------------------------------------


class TestPhase0BixbitTopologyGuard(unittest.TestCase):
    _TEST_IP = "bixbit-topology-test-ip"

    def setUp(self):
        from tuner_app import state
        from tuner_app.config.defaults import apply_defaults

        apply_defaults()
        state.MINER_CONFIGS[self._TEST_IP] = {"firmware_type": "bixbit"}

    def tearDown(self):
        from tuner_app import state

        state.MINER_CONFIGS.pop(self._TEST_IP, None)

    def test_bixbit_init_does_not_wipe_tuning_state(self):
        """chips_per_board==0 sentinel prevents topology-change wipe on Bixbit init."""
        from tuner_app.config.effective import EffectiveConfig
        from tuner_app.tuning_engine.phase_runners import phase0_discovery

        # Simulate first Phase 0: engine has placeholder (3, 108) from __init__
        engine = _make_engine("bixbit", num_boards=3, chips_per_board=108)
        # Use real EffectiveConfig so iter_all_config_keys() snapshot succeeds.
        engine.config = EffectiveConfig(self._TEST_IP)
        engine.api.summary.return_value = _make_minimal_summary()
        engine._capture_live_stock_baseline = MagicMock()
        engine._resize_board_arrays = MagicMock()
        engine._wait_for_mining_state = MagicMock()
        engine._mrr_phase6_announced = False
        engine._mrr_apply_pool_config = MagicMock()
        engine._mrr_sync = MagicMock()
        # Give the engine some pre-existing tuning data that should NOT be wiped
        engine.voltage_results = [{"voltage_mv": 14000}]
        engine.vf_surface = [{"voltage_mv": 14000, "freq_mhz": 490}]

        phase0_discovery(engine)

        # voltage_results and vf_surface must NOT be wiped
        self.assertEqual(len(engine.voltage_results), 1)
        self.assertEqual(len(engine.vf_surface), 1)


# ---------------------------------------------------------------------------
# Test 3: Phase 1 on Bixbit calls set_power_limit after V+F settle
# ---------------------------------------------------------------------------


class TestPhase1BixbitPowerLimit(unittest.TestCase):
    def test_bixbit_phase1_calls_set_power_limit(self):
        """phase1_set_voltage on Bixbit calls set_power_limit(POWER_LIMIT_W)."""
        from tuner_app.tuning_engine.apply import phase1_set_voltage

        engine = _make_engine("bixbit")
        engine._get_current_voltage_mv = MagicMock(return_value=14000)
        engine._wait_for_mining_state = MagicMock()
        engine.parked_chips = [set() for _ in range(engine.num_boards)]

        # Stub settle helpers to avoid real sleeps
        with (
            patch("tuner_app.tuning_engine.apply.wait_for_voltage_settle"),
            patch("tuner_app.tuning_engine.apply.wait_for_settle"),
        ):
            phase1_set_voltage(engine, 14000, 490)

        # set_power_limit must have been called with POWER_LIMIT_W
        engine.api.set_power_limit.assert_called_once_with(3500)

    def test_epic_phase1_does_not_call_set_power_limit(self):
        """phase1_set_voltage on ePIC does NOT call set_power_limit."""
        from tuner_app.tuning_engine.apply import phase1_set_voltage

        engine = _make_engine("epic")
        engine._get_current_voltage_mv = MagicMock(return_value=14000)
        engine._wait_for_mining_state = MagicMock()
        engine.parked_chips = [set() for _ in range(engine.num_boards)]

        with (
            patch("tuner_app.tuning_engine.apply.wait_for_voltage_settle"),
            patch("tuner_app.tuning_engine.apply.wait_for_settle"),
        ):
            phase1_set_voltage(engine, 14000, 490)

        engine.api.set_power_limit.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: Phase 2 on Bixbit populates empty per-chip baseline arrays
# ---------------------------------------------------------------------------


class TestPhase2BixbitEmptyBaselines(unittest.TestCase):
    def test_bixbit_phase2_empty_arrays(self):
        """phase2_baseline on Bixbit populates empty per-chip arrays."""
        from tuner_app.tuning_engine.phase_runners import phase2_baseline

        engine = _make_engine("bixbit", chips_per_board=0)
        engine._update_live_data = MagicMock()
        engine._detect_thermal_emergency = MagicMock(return_value=None)
        engine._park_dead_chips_from_baseline = MagicMock()
        # Prevent actual sleep — set STABILIZE_WAIT to 0 to exit the while immediately
        engine.config = dict(engine.config)
        engine.config["STABILIZE_WAIT"] = 0

        phase2_baseline(engine)

        # All per-chip arrays must be empty lists for each board
        for b in range(engine.num_boards):
            self.assertEqual(engine.baseline_scores[b], [])
            self.assertEqual(engine.baseline_chip_temps[b], [])
            self.assertEqual(engine.baseline_chip_hashrates[b], [])
            self.assertEqual(engine.baseline_freq_arrays[b], [])

        # park_dead_chips_from_baseline must still be called
        engine._park_dead_chips_from_baseline.assert_called_once()

        # hashrate() and clocks() must NOT be called (no per-chip sampling)
        engine.api.hashrate.assert_not_called()
        engine.api.clocks.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: run_phase3_phase4_at_voltage on Bixbit returns chip_tune_active=False
#         and never calls _phase3_profiling
# ---------------------------------------------------------------------------


class TestPhase3BixbitEarlyReturn(unittest.TestCase):
    def test_bixbit_skips_phase3_profiling(self):
        """run_phase3_phase4_at_voltage returns result with chip_tune_active=False on Bixbit."""
        from tuner_app.tuning_engine.chip_tune_orchestration import (
            run_phase3_phase4_at_voltage,
        )

        engine = _make_engine("bixbit", chips_per_board=0)
        engine._phase1_set_voltage = MagicMock()
        engine._apply_stable_freqs = MagicMock()
        engine._wait_for_mining_state = MagicMock()
        engine._save_checkpoint = MagicMock()
        engine._detect_thermal_emergency = MagicMock(return_value=None)
        engine._is_miner_hashing = MagicMock(return_value=True)
        engine._update_live_data = MagicMock()
        engine._get_board_temps = MagicMock(return_value=[])
        engine._get_chip_temps = MagicMock(return_value=[])

        # Stub Phase 4 measurement to return a valid efficiency dict
        fake_efficiency = {
            "efficiency_jth": 20.0,
            "hashrate_ths": 200.0,
            "power_w": 4000.0,
            "per_board": [],
        }
        with patch(
            "tuner_app.tuning_engine.chip_tune_orchestration.phase4_measure_efficiency",
            return_value=fake_efficiency,
        ):
            result = run_phase3_phase4_at_voltage(engine, 14000, 490.0, fresh_start=True)

        self.assertIsNotNone(result)
        self.assertFalse(result["chip_tune_active"])

        # Verify _phase3_profiling was NOT called (Bixbit skips it)
        engine._phase3_profiling.assert_not_called()

        # Verify per-board arrays are empty lists (no per-chip state on Bixbit)
        for arr in result["stable_freq_arrays"]:
            self.assertEqual(arr, [])
        for arr in result["baseline_scores"]:
            self.assertEqual(arr, [])

        # Result must be appended to voltage_results
        self.assertEqual(len(engine.voltage_results), 1)
        self.assertEqual(engine.voltage_results[0]["chip_tune_active"], False)


# ---------------------------------------------------------------------------
# Test 6: do_monitor_cycle_body on Bixbit skips evaluate_chip_tune_fallback
# ---------------------------------------------------------------------------


class TestPhase6BixbitSkipsFallback(unittest.TestCase):
    def test_bixbit_monitor_skips_evaluate_fallback(self):
        """do_monitor_cycle_body does not call evaluate_chip_tune_fallback on Bixbit."""
        from tuner_app.tuning_engine.monitor import do_monitor_cycle_body

        engine = _make_engine("bixbit")
        engine._mrr_phase6_announced = True
        engine._apply_stable_freqs = MagicMock()
        engine._wait_for_mining_state = MagicMock()
        engine._is_miner_hashing = MagicMock(return_value=True)
        engine._perpetual_sample_hashrate = MagicMock(return_value=200.0)
        engine._adjust_voltage = MagicMock(return_value=False)
        engine._save_profile = MagicMock()
        engine._monitor_offline_hits = 0
        engine._update_live_data = MagicMock()
        engine._detect_thermal_emergency = MagicMock(return_value=None)
        engine.active_sweep_voltage_mv = None
        engine.voltage_results = []
        engine.api.summary.return_value = _make_minimal_summary()

        # Patch long sleep and perpetual_thermal_sweep to run fast
        with (
            patch("tuner_app.tuning_engine.monitor.evaluate_chip_tune_fallback") as mock_fallback,
            patch("tuner_app.tuning_engine.monitor.perpetual_thermal_sweep", return_value=False),
            patch("tuner_app.tuning_engine.monitor.detect_thermal_emergency", return_value=None),
            patch("time.sleep"),
        ):
            # Patch the config's check_min to 0 so check_interval_sec=0 and sleep loop skips
            engine.config = dict(engine.config)
            engine.config["PERPETUAL_VOLTAGE_CHECK_MIN"] = 0

            do_monitor_cycle_body(engine)

            # evaluate_chip_tune_fallback must NOT have been called for Bixbit
            mock_fallback.assert_not_called()

    def test_epic_monitor_calls_evaluate_fallback(self):
        """do_monitor_cycle_body DOES call evaluate_chip_tune_fallback for ePIC."""
        from tuner_app.tuning_engine.monitor import do_monitor_cycle_body

        engine = _make_engine("epic")
        engine._mrr_phase6_announced = True
        engine._apply_stable_freqs = MagicMock()
        engine._wait_for_mining_state = MagicMock()
        engine._is_miner_hashing = MagicMock(return_value=True)
        engine._perpetual_sample_hashrate = MagicMock(return_value=200.0)
        engine._adjust_voltage = MagicMock(return_value=False)
        engine._save_profile = MagicMock()
        engine._monitor_offline_hits = 0
        engine._update_live_data = MagicMock()
        engine._detect_thermal_emergency = MagicMock(return_value=None)
        engine.active_sweep_voltage_mv = None
        engine.voltage_results = []
        engine.api.summary.return_value = _make_minimal_summary()

        with (
            patch("tuner_app.tuning_engine.monitor.evaluate_chip_tune_fallback") as mock_fallback,
            patch("tuner_app.tuning_engine.monitor.perpetual_thermal_sweep", return_value=False),
            patch("tuner_app.tuning_engine.monitor.detect_thermal_emergency", return_value=None),
            patch("time.sleep"),
        ):
            engine.config = dict(engine.config)
            engine.config["PERPETUAL_VOLTAGE_CHECK_MIN"] = 0

            do_monitor_cycle_body(engine)

            # evaluate_chip_tune_fallback MUST have been called for ePIC
            mock_fallback.assert_called_once()


# ---------------------------------------------------------------------------
# Test 7: park_dead_chips_from_baseline short-circuits when chips_per_board==0
# ---------------------------------------------------------------------------


class TestParkDeadChipsShortCircuit(unittest.TestCase):
    def test_zero_chips_per_board_early_return(self):
        """park_dead_chips_from_baseline returns immediately when chips_per_board==0."""
        from tuner_app.tuning_engine.reset import park_dead_chips_from_baseline

        engine = _make_engine("bixbit", chips_per_board=0)
        engine.baseline_scores = [[], [], []]

        # park_dead_chips_from_baseline must not attempt to read config keys
        # that would normally be accessed (DEAD_CHIP_SCORE, DEAD_CHIP_FREQ).
        # We replace config with an object that raises KeyError on any access
        # to confirm early-return before those reads.
        class _ErrorConfig(dict):
            def __getitem__(self, k):
                raise AssertionError(f"Should not read config[{k!r}] when chips_per_board==0")

        engine.config = _ErrorConfig()
        engine.chips_per_board = 0

        # Must not raise
        park_dead_chips_from_baseline(engine)

    def test_nonzero_chips_per_board_proceeds(self):
        """park_dead_chips_from_baseline proceeds normally when chips_per_board > 0."""
        from tuner_app.tuning_engine.reset import park_dead_chips_from_baseline

        engine = _make_engine("epic", chips_per_board=108)
        engine.baseline_scores = [[80.0] * 3]  # 1 board, 3 chips all healthy
        engine.num_boards = 1
        engine.chips_per_board = 3
        engine.parked_chips = [set()]
        engine.proposed_freqs = [[490.0, 490.0, 490.0]]
        engine.stable_freq_arrays = [[490.0, 490.0, 490.0]]
        engine.config = {"DEAD_CHIP_SCORE": 20, "DEAD_CHIP_FREQ": 50}

        park_dead_chips_from_baseline(engine)

        # All chips healthy → no chips parked
        self.assertEqual(len(engine.parked_chips[0]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
