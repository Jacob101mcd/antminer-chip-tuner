"""Tests for Phase 0 stock baseline sampling honoring STOCK_BASELINE_SAMPLES
and STOCK_BASELINE_INTERVAL config knobs.

Regression: pre-fix, the loop hardcoded `range(5)` and `time.sleep(10)`,
yielding a fixed ~40s window regardless of config. The fix wires the loop
to engine.config["STOCK_BASELINE_SAMPLES"] / ["STOCK_BASELINE_INTERVAL"].
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tuner_app.miner.types import MinerSummary
from tuner_app.tuning_engine.lifecycle import capture_live_stock_baseline


def _make_summary(hr: float = 200.0, power: float = 3500.0, voltage: float = 14000.0):
    return MinerSummary(
        operating_state="Mining",
        hashrate_ths=hr,
        power_w=power,
        fan_speed=4500,
        target_voltage_mv=voltage,
        output_voltage_mv=voltage / 1000.0,
        hostname="miner-example",
        model="S21",
        boards=[],
        raw={},
    )


def _make_engine(samples: int, interval: int):
    """Engine mock that exercises the live-sampling loop."""
    engine = MagicMock()
    engine.running = True
    engine.stock_baseline = None  # No prior baseline -> capture proceeds
    engine.num_boards = 3
    engine.chips_per_board = 108
    engine.STOCK_SPEC = {
        "hashrate_ths": 200.0,
        "power_w": 3500.0,
        "efficiency_jth": 17.5,
        "freq_mhz": 490,
    }
    engine.config = {
        "STOCK_BASELINE_SAMPLES": samples,
        "STOCK_BASELINE_INTERVAL": interval,
    }
    # api.summary / clocks / hashrate / temps_chip return objects shaped enough
    # that the per-chip accumulator try/except blocks don't error meaningfully.
    engine.api = MagicMock()
    engine.api.summary.return_value = _make_summary()
    engine.api.clocks.return_value = []
    engine.api.hashrate.return_value = []
    engine.api.temps_chip.return_value = []
    return engine


class TestStockBaselineSamplingConfig(unittest.TestCase):
    def test_default_5_samples_40s_interval(self):
        engine = _make_engine(samples=5, interval=40)
        with patch("tuner_app.tuning_engine.lifecycle.time.sleep") as mock_sleep:
            capture_live_stock_baseline(engine, _make_summary())
        # 5 samples → 4 inter-sample sleeps, each 40s.
        self.assertEqual(mock_sleep.call_count, 4)
        for call in mock_sleep.call_args_list:
            self.assertEqual(call.args, (40,))

    def test_custom_samples_and_interval(self):
        engine = _make_engine(samples=8, interval=15)
        with patch("tuner_app.tuning_engine.lifecycle.time.sleep") as mock_sleep:
            capture_live_stock_baseline(engine, _make_summary())
        self.assertEqual(mock_sleep.call_count, 7)
        for call in mock_sleep.call_args_list:
            self.assertEqual(call.args, (15,))

    def test_single_sample_no_sleeps(self):
        engine = _make_engine(samples=1, interval=40)
        with patch("tuner_app.tuning_engine.lifecycle.time.sleep") as mock_sleep:
            capture_live_stock_baseline(engine, _make_summary())
        self.assertEqual(mock_sleep.call_count, 0)

    def test_log_message_reflects_total_window(self):
        engine = _make_engine(samples=5, interval=40)
        with patch("tuner_app.tuning_engine.lifecycle.time.sleep"):
            capture_live_stock_baseline(engine, _make_summary())
        # Log message should mention "5x over 160s" (4 sleeps * 40s = 160s).
        log_msgs = [c.args[0] for c in engine.log.call_args_list if c.args]
        capture_msg = next((m for m in log_msgs if "Capturing live stock baseline" in m), None)
        self.assertIsNotNone(capture_msg)
        self.assertIn("5x over 160s", capture_msg)


if __name__ == "__main__":
    unittest.main()
