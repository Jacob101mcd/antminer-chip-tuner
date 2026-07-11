"""Tests for the Phase V settle-failure cell-skip in
``measurement.measure_vf_point``.

When ``_phase1_set_voltage`` raises ``MinerNotReady`` mid-measurement
(typically because the PSU can't reach the commanded voltage within
``SETTLE_VOLTAGE_TOLERANCE_MV``), ``measure_vf_point`` records a
sentinel entry in ``engine.vf_surface`` with ``thermal_failed=True``
and ``settle_failed=True`` BEFORE re-raising. ``score_cell`` filters
``thermal_failed`` cells out of ranking and trend walks, so on the
next iteration of the state machine ``find_next_coarse_to_measure``
walks past the failed cell and picks a different unmeasured cell.

Without this fix, an unreachable PSU-max grid cell re-fires Phase 1
on every retry-loop iteration regardless of the retry-counter
gating — a supervised test miner accumulated repeated ``Voltage settle
timeout`` events over a single day.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuner_app.miner.exceptions import MinerCommandError, MinerNotReady


class TestMeasureVfPointSettleFailedSentinel(unittest.TestCase):
    def _make_engine(self):
        engine = MagicMock()
        engine.running = True
        engine.vf_surface = []
        engine.current_sweep_voltage_mv = None
        engine.current_vf_point = None
        engine.config = {
            "VF_EXPLORE_WAIT": 60,
            "VF_EXPLORE_SAMPLES": 5,
            "VF_EXPLORE_SAMPLE_INTERVAL": 30,
        }
        # Make _save_checkpoint a no-op MagicMock so we can assert it was called.
        engine._save_checkpoint = MagicMock()
        engine.log = MagicMock()
        return engine

    def test_settle_failure_appends_thermal_failed_entry(self):
        from tuner_app.tuning_engine import measurement

        engine = self._make_engine()
        engine._phase1_set_voltage = MagicMock(
            side_effect=MinerNotReady("Miner failed to settle after 20 attempts (600s)")
        )

        with self.assertRaises(MinerNotReady):
            measurement.measure_vf_point(engine, 15182, 525.0, fine=False)

        # Exactly one sentinel cell appended.
        self.assertEqual(len(engine.vf_surface), 1)
        entry = engine.vf_surface[0]
        self.assertEqual(entry["voltage_mv"], 15182)
        self.assertEqual(entry["freq_mhz"], 525.0)
        # Both flags must be set: thermal_failed for score_cell filtering,
        # settle_failed for the dashboard / operator to know what happened.
        self.assertTrue(entry["thermal_failed"])
        self.assertTrue(entry["settle_failed"])
        # Efficiency/hashrate/power zeroed (consistent with thermal_failed sentinel).
        self.assertEqual(entry["efficiency_jth"], 0)
        self.assertEqual(entry["hashrate_ths"], 0)
        self.assertEqual(entry["power_w"], 0)
        # fine flag round-trips.
        self.assertFalse(entry["fine"])
        # Timestamp present (ISO-formatted).
        self.assertIn("measured_at", entry)

    def test_settle_failure_re_raises_after_appending(self):
        from tuner_app.tuning_engine import measurement

        engine = self._make_engine()
        boom = MinerNotReady("settle timeout")
        engine._phase1_set_voltage = MagicMock(side_effect=boom)

        with self.assertRaises(MinerNotReady) as ctx:
            measurement.measure_vf_point(engine, 15182, 525.0)

        # Same exception instance propagates so the outer retry loop sees it.
        self.assertIs(ctx.exception, boom)

    def test_settle_failure_saves_checkpoint(self):
        from tuner_app.tuning_engine import measurement

        engine = self._make_engine()
        engine._phase1_set_voltage = MagicMock(side_effect=MinerNotReady("nope"))

        with self.assertRaises(MinerNotReady):
            measurement.measure_vf_point(engine, 15182, 525.0)

        engine._save_checkpoint.assert_called_once()

    def test_settle_failure_clears_in_flight_dashboard_state(self):
        from tuner_app.tuning_engine import measurement

        engine = self._make_engine()
        engine._phase1_set_voltage = MagicMock(side_effect=MinerNotReady("nope"))

        with self.assertRaises(MinerNotReady):
            measurement.measure_vf_point(engine, 15182, 525.0)

        # Both the per-cell pulse marker and the sweep-voltage tag must be
        # cleared so the dashboard doesn't keep flashing the failed cell.
        self.assertIsNone(engine.current_vf_point)
        self.assertIsNone(engine.current_sweep_voltage_mv)

    def test_checkpoint_failure_does_not_swallow_minernotready(self):
        """If _save_checkpoint itself blows up after the sentinel is appended,
        we still re-raise the original MinerNotReady — the recovery cycle
        must run."""
        from tuner_app.tuning_engine import measurement

        engine = self._make_engine()
        engine._phase1_set_voltage = MagicMock(side_effect=MinerNotReady("nope"))
        engine._save_checkpoint = MagicMock(side_effect=OSError("disk full"))

        with self.assertRaises(MinerNotReady):
            measurement.measure_vf_point(engine, 15182, 525.0)

        # Sentinel still appended even though checkpoint save failed.
        self.assertEqual(len(engine.vf_surface), 1)
        self.assertTrue(engine.vf_surface[0]["settle_failed"])

    def test_minercommanderror_does_NOT_get_sentinel(self):
        """Only MinerNotReady — the specific settle-timeout signal — should
        produce a settle_failed sentinel. MinerCommandError (HTTP-layer
        failure) is transient and should propagate without marking the cell
        as unmeasurable."""
        from tuner_app.tuning_engine import measurement

        engine = self._make_engine()
        engine._phase1_set_voltage = MagicMock(
            side_effect=MinerCommandError("POST /miner: 503 Service Unavailable")
        )

        with self.assertRaises(MinerCommandError):
            measurement.measure_vf_point(engine, 15182, 525.0)

        # vf_surface stays empty — the cell is still unmeasured, not failed.
        self.assertEqual(len(engine.vf_surface), 0)


class TestSettleFailedIsFilteredFromScoring(unittest.TestCase):
    """Verify that the settle_failed sentinel actually gets skipped by the
    state machine's coarse-cell ranking. The sentinel uses
    thermal_failed=True specifically so score_cell's existing filter
    catches it without additional changes — this test locks that in."""

    def test_score_cell_returns_none_for_settle_failed_entry(self):
        from tuner_app.profit.compute import score_cell

        entry = {
            "voltage_mv": 15182,
            "freq_mhz": 525.0,
            "efficiency_jth": 0,
            "hashrate_ths": 0,
            "power_w": 0,
            "fine": False,
            "thermal_failed": True,
            "settle_failed": True,
            "measured_at": "2026-05-10T21:00:00",
        }
        # Both modes should return None — the cell is unmeasurable.
        self.assertIsNone(
            score_cell(
                entry,
                target_mode="efficiency",
                electric_rate=0.075,
                coin_data=None,
            )
        )
        self.assertIsNone(
            score_cell(
                entry,
                target_mode="profitability",
                electric_rate=0.075,
                coin_data={"price_usd": 60000, "reward_block": 6.25, "difficulty": 1e14},
            )
        )


if __name__ == "__main__":
    unittest.main()
