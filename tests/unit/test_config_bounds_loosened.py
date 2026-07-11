"""Unit tests verifying loosened upper bounds on time-based CONFIG_BOUNDS knobs.

Exercises the following sixteen keys whose upper bounds are being raised:

Second-keys (new upper bound 31_536_000):
  STABILIZE_WAIT, BASELINE_INTERVAL, ROUND_INTERVAL, STOCK_BASELINE_INTERVAL,
  VF_EXPLORE_WAIT, VF_EXPLORE_SAMPLE_INTERVAL, STABILITY_POLISH_ROUND_INTERVAL,
  STABILITY_POLISH_STABILIZE_WAIT, BRAIINS_TUNER_STABILIZE_WAIT_SEC,
  OFFLINE_POLL_INTERVAL, RESET_STOP_WAIT, RESET_START_WAIT,
  EFFICIENCY_MEASURE_WAIT

Minute-keys (new upper bound 525_600):
  PERPETUAL_VOLTAGE_CHECK_MIN, SCAN_INTERVAL_MIN

Hour-keys (new upper bound 8760):
  PERPETUAL_RESTART_MIN_HOURS

Also includes four excluded-key guard tests that verify intentionally-tight
upper bounds on LUXOS_MIN_CONN_INTERVAL_SEC (5.0), LUXOS_OFFLINE_BACKOFF_SEC
(300), SCAN_TIMEOUT_SEC (30), and LOG_DEDUP_WINDOW_SEC (60) are NOT widened.
"""

from __future__ import annotations

import unittest

from tuner_app.config.validation import validate_config


class TestSecondKeyBoundsLoosened(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # STABILIZE_WAIT  (old upper 600, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_stabilize_wait_at_old_upper_now_accepted(self):
        """STABILIZE_WAIT at old upper bound 600 is accepted after loosening."""
        _, errors = validate_config({"STABILIZE_WAIT": 600})
        self.assertEqual(errors, [])

    def test_stabilize_wait_at_new_upper_accepted(self):
        """STABILIZE_WAIT at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"STABILIZE_WAIT": 31_536_000})
        self.assertEqual(errors, [])

    def test_stabilize_wait_above_new_upper_rejected(self):
        """STABILIZE_WAIT above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"STABILIZE_WAIT": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # BASELINE_INTERVAL  (old upper 300, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_baseline_interval_at_old_upper_now_accepted(self):
        """BASELINE_INTERVAL at old upper bound 300 is accepted after loosening."""
        _, errors = validate_config({"BASELINE_INTERVAL": 300})
        self.assertEqual(errors, [])

    def test_baseline_interval_at_new_upper_accepted(self):
        """BASELINE_INTERVAL at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"BASELINE_INTERVAL": 31_536_000})
        self.assertEqual(errors, [])

    def test_baseline_interval_above_new_upper_rejected(self):
        """BASELINE_INTERVAL above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"BASELINE_INTERVAL": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # ROUND_INTERVAL  (old upper 300, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_round_interval_at_old_upper_now_accepted(self):
        """ROUND_INTERVAL at old upper bound 300 is accepted after loosening."""
        _, errors = validate_config({"ROUND_INTERVAL": 300})
        self.assertEqual(errors, [])

    def test_round_interval_at_new_upper_accepted(self):
        """ROUND_INTERVAL at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"ROUND_INTERVAL": 31_536_000})
        self.assertEqual(errors, [])

    def test_round_interval_above_new_upper_rejected(self):
        """ROUND_INTERVAL above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"ROUND_INTERVAL": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # STOCK_BASELINE_INTERVAL  (old upper 300, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_stock_baseline_interval_at_old_upper_now_accepted(self):
        """STOCK_BASELINE_INTERVAL at old upper bound 300 is accepted after loosening."""
        _, errors = validate_config({"STOCK_BASELINE_INTERVAL": 300})
        self.assertEqual(errors, [])

    def test_stock_baseline_interval_at_new_upper_accepted(self):
        """STOCK_BASELINE_INTERVAL at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"STOCK_BASELINE_INTERVAL": 31_536_000})
        self.assertEqual(errors, [])

    def test_stock_baseline_interval_above_new_upper_rejected(self):
        """STOCK_BASELINE_INTERVAL above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"STOCK_BASELINE_INTERVAL": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # VF_EXPLORE_WAIT  (old upper 600, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_vf_explore_wait_at_old_upper_now_accepted(self):
        """VF_EXPLORE_WAIT at old upper bound 600 is accepted after loosening."""
        _, errors = validate_config({"VF_EXPLORE_WAIT": 600})
        self.assertEqual(errors, [])

    def test_vf_explore_wait_at_new_upper_accepted(self):
        """VF_EXPLORE_WAIT at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"VF_EXPLORE_WAIT": 31_536_000})
        self.assertEqual(errors, [])

    def test_vf_explore_wait_above_new_upper_rejected(self):
        """VF_EXPLORE_WAIT above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"VF_EXPLORE_WAIT": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # VF_EXPLORE_SAMPLE_INTERVAL  (old upper 60, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_vf_explore_sample_interval_at_old_upper_now_accepted(self):
        """VF_EXPLORE_SAMPLE_INTERVAL at old upper bound 60 is accepted after loosening."""
        _, errors = validate_config({"VF_EXPLORE_SAMPLE_INTERVAL": 60})
        self.assertEqual(errors, [])

    def test_vf_explore_sample_interval_at_new_upper_accepted(self):
        """VF_EXPLORE_SAMPLE_INTERVAL at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"VF_EXPLORE_SAMPLE_INTERVAL": 31_536_000})
        self.assertEqual(errors, [])

    def test_vf_explore_sample_interval_above_new_upper_rejected(self):
        """VF_EXPLORE_SAMPLE_INTERVAL above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"VF_EXPLORE_SAMPLE_INTERVAL": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # STABILITY_POLISH_ROUND_INTERVAL  (old upper 300, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_stability_polish_round_interval_at_old_upper_now_accepted(self):
        """STABILITY_POLISH_ROUND_INTERVAL at old upper bound 300 is accepted after loosening."""
        _, errors = validate_config({"STABILITY_POLISH_ROUND_INTERVAL": 300})
        self.assertEqual(errors, [])

    def test_stability_polish_round_interval_at_new_upper_accepted(self):
        """STABILITY_POLISH_ROUND_INTERVAL at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"STABILITY_POLISH_ROUND_INTERVAL": 31_536_000})
        self.assertEqual(errors, [])

    def test_stability_polish_round_interval_above_new_upper_rejected(self):
        """STABILITY_POLISH_ROUND_INTERVAL above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"STABILITY_POLISH_ROUND_INTERVAL": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # STABILITY_POLISH_STABILIZE_WAIT  (old upper 1800, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_stability_polish_stabilize_wait_at_old_upper_now_accepted(self):
        """STABILITY_POLISH_STABILIZE_WAIT at old upper bound 1800 is accepted after loosening."""
        _, errors = validate_config({"STABILITY_POLISH_STABILIZE_WAIT": 1800})
        self.assertEqual(errors, [])

    def test_stability_polish_stabilize_wait_at_new_upper_accepted(self):
        """STABILITY_POLISH_STABILIZE_WAIT at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"STABILITY_POLISH_STABILIZE_WAIT": 31_536_000})
        self.assertEqual(errors, [])

    def test_stability_polish_stabilize_wait_above_new_upper_rejected(self):
        """STABILITY_POLISH_STABILIZE_WAIT above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"STABILITY_POLISH_STABILIZE_WAIT": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # BRAIINS_TUNER_STABILIZE_WAIT_SEC  (old upper 3600, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_braiins_tuner_stabilize_wait_sec_at_old_upper_now_accepted(self):
        """BRAIINS_TUNER_STABILIZE_WAIT_SEC at old upper bound 3600 is accepted after loosening."""
        _, errors = validate_config({"BRAIINS_TUNER_STABILIZE_WAIT_SEC": 3600})
        self.assertEqual(errors, [])

    def test_braiins_tuner_stabilize_wait_sec_at_new_upper_accepted(self):
        """BRAIINS_TUNER_STABILIZE_WAIT_SEC at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"BRAIINS_TUNER_STABILIZE_WAIT_SEC": 31_536_000})
        self.assertEqual(errors, [])

    def test_braiins_tuner_stabilize_wait_sec_above_new_upper_rejected(self):
        """BRAIINS_TUNER_STABILIZE_WAIT_SEC above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"BRAIINS_TUNER_STABILIZE_WAIT_SEC": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # OFFLINE_POLL_INTERVAL  (old upper 300, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_offline_poll_interval_at_old_upper_now_accepted(self):
        """OFFLINE_POLL_INTERVAL at old upper bound 300 is accepted after loosening."""
        _, errors = validate_config({"OFFLINE_POLL_INTERVAL": 300})
        self.assertEqual(errors, [])

    def test_offline_poll_interval_at_new_upper_accepted(self):
        """OFFLINE_POLL_INTERVAL at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"OFFLINE_POLL_INTERVAL": 31_536_000})
        self.assertEqual(errors, [])

    def test_offline_poll_interval_above_new_upper_rejected(self):
        """OFFLINE_POLL_INTERVAL above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"OFFLINE_POLL_INTERVAL": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # RESET_STOP_WAIT  (old upper 600, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_reset_stop_wait_at_old_upper_now_accepted(self):
        """RESET_STOP_WAIT at old upper bound 600 is accepted after loosening."""
        _, errors = validate_config({"RESET_STOP_WAIT": 600})
        self.assertEqual(errors, [])

    def test_reset_stop_wait_at_new_upper_accepted(self):
        """RESET_STOP_WAIT at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"RESET_STOP_WAIT": 31_536_000})
        self.assertEqual(errors, [])

    def test_reset_stop_wait_above_new_upper_rejected(self):
        """RESET_STOP_WAIT above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"RESET_STOP_WAIT": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # RESET_START_WAIT  (old upper 1200, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_reset_start_wait_at_old_upper_now_accepted(self):
        """RESET_START_WAIT at old upper bound 1200 is accepted after loosening."""
        _, errors = validate_config({"RESET_START_WAIT": 1200})
        self.assertEqual(errors, [])

    def test_reset_start_wait_at_new_upper_accepted(self):
        """RESET_START_WAIT at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"RESET_START_WAIT": 31_536_000})
        self.assertEqual(errors, [])

    def test_reset_start_wait_above_new_upper_rejected(self):
        """RESET_START_WAIT above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"RESET_START_WAIT": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))

    # ------------------------------------------------------------------ #
    # EFFICIENCY_MEASURE_WAIT  (old upper 600, new upper 31_536_000)
    # ------------------------------------------------------------------ #

    def test_efficiency_measure_wait_at_old_upper_now_accepted(self):
        """EFFICIENCY_MEASURE_WAIT at old upper bound 600 is accepted after loosening."""
        _, errors = validate_config({"EFFICIENCY_MEASURE_WAIT": 600})
        self.assertEqual(errors, [])

    def test_efficiency_measure_wait_at_new_upper_accepted(self):
        """EFFICIENCY_MEASURE_WAIT at new upper bound 31_536_000 is accepted."""
        _, errors = validate_config({"EFFICIENCY_MEASURE_WAIT": 31_536_000})
        self.assertEqual(errors, [])

    def test_efficiency_measure_wait_above_new_upper_rejected(self):
        """EFFICIENCY_MEASURE_WAIT above 31_536_000 produces a bounds error."""
        _, errors = validate_config({"EFFICIENCY_MEASURE_WAIT": 31_536_001})
        self.assertTrue(any("31536000" in e for e in errors))


class TestMinuteKeyBoundsLoosened(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # PERPETUAL_VOLTAGE_CHECK_MIN  (old upper 120, new upper 525_600)
    # ------------------------------------------------------------------ #

    def test_perpetual_voltage_check_min_at_old_upper_now_accepted(self):
        """PERPETUAL_VOLTAGE_CHECK_MIN at old upper bound 120 is accepted after loosening."""
        _, errors = validate_config({"PERPETUAL_VOLTAGE_CHECK_MIN": 120})
        self.assertEqual(errors, [])

    def test_perpetual_voltage_check_min_at_new_upper_accepted(self):
        """PERPETUAL_VOLTAGE_CHECK_MIN at new upper bound 525_600 is accepted."""
        _, errors = validate_config({"PERPETUAL_VOLTAGE_CHECK_MIN": 525_600})
        self.assertEqual(errors, [])

    def test_perpetual_voltage_check_min_above_new_upper_rejected(self):
        """PERPETUAL_VOLTAGE_CHECK_MIN above 525_600 produces a bounds error."""
        _, errors = validate_config({"PERPETUAL_VOLTAGE_CHECK_MIN": 525_601})
        self.assertTrue(any("525600" in e for e in errors))

    # ------------------------------------------------------------------ #
    # SCAN_INTERVAL_MIN  (old upper 1440, new upper 525_600)
    # ------------------------------------------------------------------ #

    def test_scan_interval_min_at_old_upper_now_accepted(self):
        """SCAN_INTERVAL_MIN at old upper bound 1440 is accepted after loosening."""
        _, errors = validate_config({"SCAN_INTERVAL_MIN": 1440})
        self.assertEqual(errors, [])

    def test_scan_interval_min_at_new_upper_accepted(self):
        """SCAN_INTERVAL_MIN at new upper bound 525_600 is accepted."""
        _, errors = validate_config({"SCAN_INTERVAL_MIN": 525_600})
        self.assertEqual(errors, [])

    def test_scan_interval_min_above_new_upper_rejected(self):
        """SCAN_INTERVAL_MIN above 525_600 produces a bounds error."""
        _, errors = validate_config({"SCAN_INTERVAL_MIN": 525_601})
        self.assertTrue(any("525600" in e for e in errors))


class TestHourKeyBoundsLoosened(unittest.TestCase):
    # ------------------------------------------------------------------ #
    # PERPETUAL_RESTART_MIN_HOURS  (old upper 168, new upper 8760)
    # ------------------------------------------------------------------ #

    def test_perpetual_restart_min_hours_at_old_upper_now_accepted(self):
        """PERPETUAL_RESTART_MIN_HOURS at old upper bound 168 is accepted after loosening."""
        _, errors = validate_config({"PERPETUAL_RESTART_MIN_HOURS": 168})
        self.assertEqual(errors, [])

    def test_perpetual_restart_min_hours_at_new_upper_accepted(self):
        """PERPETUAL_RESTART_MIN_HOURS at new upper bound 8760 is accepted."""
        _, errors = validate_config({"PERPETUAL_RESTART_MIN_HOURS": 8760})
        self.assertEqual(errors, [])

    def test_perpetual_restart_min_hours_above_new_upper_rejected(self):
        """PERPETUAL_RESTART_MIN_HOURS above 8760 produces a bounds error."""
        _, errors = validate_config({"PERPETUAL_RESTART_MIN_HOURS": 8761})
        self.assertTrue(any("8760" in e for e in errors))


class TestExcludedKeysUntouched(unittest.TestCase):
    """Guard tests: intentionally-tight upper bounds must not be widened."""

    def test_luxos_min_conn_interval_sec_upper_bound_preserved(self):
        """LUXOS_MIN_CONN_INTERVAL_SEC above 5.0 produces an error mentioning 5.0."""
        _, errors = validate_config({"LUXOS_MIN_CONN_INTERVAL_SEC": 5.1})
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("5.0" in e for e in errors))

    def test_luxos_offline_backoff_sec_upper_bound_preserved(self):
        """LUXOS_OFFLINE_BACKOFF_SEC above 300 produces an error mentioning 300."""
        _, errors = validate_config({"LUXOS_OFFLINE_BACKOFF_SEC": 301})
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("300" in e for e in errors))

    def test_scan_timeout_sec_upper_bound_preserved(self):
        """SCAN_TIMEOUT_SEC above 30.0 produces an error mentioning 30."""
        _, errors = validate_config({"SCAN_TIMEOUT_SEC": 31})
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("30" in e for e in errors))

    def test_log_dedup_window_sec_upper_bound_preserved(self):
        """LOG_DEDUP_WINDOW_SEC above 60 produces an error mentioning 60."""
        _, errors = validate_config({"LOG_DEDUP_WINDOW_SEC": 61})
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("60" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
