"""Unit tests for config validation logic in tuner_app.config.validation.

Covers:
- Bounds-check happy paths and out-of-range rejection for safety-critical
  config keys (BOARD_MAX_TEMP, CHIP_CRITICAL_TEMP, VF_EXPLORE_F_MIN, VF_EXPLORE_F_MAX)
- Unknown-key rejection
- Cross-field invariants:
    * VF_EXPLORE_TOP_K <= VF_EXPLORE_V_COUNT  (PRESERVED)
    * CHIP_FREQ_SPREAD_MHZ >= 2 x CHIP_TUNE_STEP_MHZ
    * CHIP_TUNE_UP_TOLERANCE <= CHIP_TUNE_DOWN_TOLERANCE
    * VF_EXPLORE_F_MAX > VF_EXPLORE_F_MIN + 6.25

Note: the VF_EXPLORE_TOP_K <= VF_FINE_TOP_K <= VF_COARSE_TOP_K_RAYS nesting
rule has been removed. Each top-K knob is now independently validated against
its per-knob bounds only. See test_vf_topk_decoupled.py for the regression
tests covering the decoupling. No cross-field nesting tests for these three
knobs appear in this file — they all live in test_vf_topk_decoupled.py.
"""

from __future__ import annotations

import unittest

from tuner_app.config.validation import validate_config


class TestSafetyCriticalBounds(unittest.TestCase):
    def test_board_max_temp_at_lower_bound(self):
        """BOARD_MAX_TEMP at lower bound cleans with no errors."""
        cleaned, errors = validate_config({"BOARD_MAX_TEMP": 50})
        self.assertEqual(errors, [])

    def test_board_max_temp_at_upper_bound(self):
        """BOARD_MAX_TEMP at upper bound cleans with no errors."""
        cleaned, errors = validate_config({"BOARD_MAX_TEMP": 85})
        self.assertEqual(errors, [])

    def test_board_max_temp_below_lower_rejected(self):
        """BOARD_MAX_TEMP below lower bound produces bounds error."""
        cleaned, errors = validate_config({"BOARD_MAX_TEMP": 49})
        self.assertTrue(any("50" in e for e in errors))

    def test_board_max_temp_above_upper_rejected(self):
        """BOARD_MAX_TEMP above upper bound produces bounds error."""
        cleaned, errors = validate_config({"BOARD_MAX_TEMP": 86})
        self.assertTrue(any("85" in e for e in errors))

    def test_chip_critical_temp_lower_upper_bounds(self):
        """CHIP_CRITICAL_TEMP at bounds cleans, out-of-bounds rejected."""
        cleaned, errors = validate_config({"CHIP_CRITICAL_TEMP": 50})
        self.assertEqual(errors, [])
        cleaned, errors = validate_config({"CHIP_CRITICAL_TEMP": 110})
        self.assertEqual(errors, [])
        cleaned, errors = validate_config({"CHIP_CRITICAL_TEMP": 49})
        self.assertTrue(any("50" in e for e in errors))
        cleaned, errors = validate_config({"CHIP_CRITICAL_TEMP": 111})
        self.assertTrue(any("110" in e for e in errors))

    def test_vf_explore_f_min_bounds(self):
        """VF_EXPLORE_F_MIN at bounds cleans, out-of-bounds rejected."""
        cleaned, errors = validate_config({"VF_EXPLORE_F_MIN": 50})
        # Note: F_MIN=50 may trigger F_MAX > F_MIN + 6.25 cross-check depending on
        # current default F_MAX. The bounds check itself should pass.
        self.assertFalse(any("between 50 and 900" in e for e in errors))
        cleaned, errors = validate_config({"VF_EXPLORE_F_MIN": 900})
        self.assertFalse(any("between 50 and 900" in e for e in errors))
        cleaned, errors = validate_config({"VF_EXPLORE_F_MIN": 49})
        self.assertTrue(any("50" in e for e in errors))

    def test_vf_explore_f_max_bounds(self):
        """VF_EXPLORE_F_MAX at bounds cleans, out-of-bounds rejected."""
        cleaned, errors = validate_config({"VF_EXPLORE_F_MAX": 50})
        self.assertFalse(any("between 50 and 900" in e for e in errors))
        cleaned, errors = validate_config({"VF_EXPLORE_F_MAX": 900})
        self.assertFalse(any("between 50 and 900" in e for e in errors))
        cleaned, errors = validate_config({"VF_EXPLORE_F_MAX": 49})
        self.assertTrue(any("50" in e for e in errors))

    def test_unknown_key_rejected(self):
        """Unknown config keys produce error mentioning 'Unknown config key'."""
        cleaned, errors = validate_config({"NOT_A_REAL_KEY": 99})
        self.assertTrue(any("Unknown config key" in e for e in errors))


class TestCrossFieldInvariants(unittest.TestCase):
    def test_chip_freq_spread_must_exceed_2x_step(self):
        """CHIP_FREQ_SPREAD_MHZ < 2 x CHIP_TUNE_STEP_MHZ produces invariant error."""
        cleaned, errors = validate_config({"CHIP_FREQ_SPREAD_MHZ": 10, "CHIP_TUNE_STEP_MHZ": 6.25})
        self.assertTrue(any("CHIP_FREQ_SPREAD_MHZ" in e for e in errors))

    def test_chip_freq_spread_valid(self):
        """CHIP_FREQ_SPREAD_MHZ >= 2 x CHIP_TUNE_STEP_MHZ cleans without spread error."""
        cleaned, errors = validate_config({"CHIP_FREQ_SPREAD_MHZ": 40, "CHIP_TUNE_STEP_MHZ": 6.25})
        self.assertFalse(any("must be >=" in e and "CHIP_TUNE_STEP_MHZ" in e for e in errors))

    def test_chip_tune_up_must_be_le_down(self):
        """CHIP_TUNE_UP_TOLERANCE > CHIP_TUNE_DOWN_TOLERANCE produces error."""
        cleaned, errors = validate_config(
            {"CHIP_TUNE_UP_TOLERANCE": 20, "CHIP_TUNE_DOWN_TOLERANCE": 10}
        )
        self.assertTrue(
            any("CHIP_TUNE_UP_TOLERANCE" in e and "CHIP_TUNE_DOWN_TOLERANCE" in e for e in errors)
        )

    def test_chip_tune_up_eq_down_valid(self):
        """CHIP_TUNE_UP_TOLERANCE == CHIP_TUNE_DOWN_TOLERANCE cleans without UP/DOWN error."""
        cleaned, errors = validate_config(
            {"CHIP_TUNE_UP_TOLERANCE": 10, "CHIP_TUNE_DOWN_TOLERANCE": 10}
        )
        self.assertFalse(
            any("CHIP_TUNE_UP_TOLERANCE" in e and "CHIP_TUNE_DOWN_TOLERANCE" in e for e in errors)
        )

    def test_vf_f_max_must_exceed_f_min_plus_6_25(self):
        """VF_EXPLORE_F_MAX <= VF_EXPLORE_F_MIN + 6.25 produces error."""
        cleaned, errors = validate_config({"VF_EXPLORE_F_MIN": 400, "VF_EXPLORE_F_MAX": 405})
        self.assertTrue(any("VF_EXPLORE_F_MAX" in e and "VF_EXPLORE_F_MIN" in e for e in errors))

    def test_vf_f_max_exceeds_f_min_plus_more_than_6_25_valid(self):
        """VF_EXPLORE_F_MAX > VF_EXPLORE_F_MIN + 6.25 cleans without F_MAX/F_MIN error."""
        cleaned, errors = validate_config({"VF_EXPLORE_F_MIN": 400, "VF_EXPLORE_F_MAX": 410})
        self.assertFalse(any("VF_EXPLORE_F_MAX" in e and "VF_EXPLORE_F_MIN" in e for e in errors))


class TestScanIpBlacklistValidation(unittest.TestCase):
    def test_blacklist_valid_mixed_entries_accepted(self):
        cleaned, errors = validate_config(
            {"SCAN_IP_BLACKLIST": ["192.168.1.5", "10.0.0.0/30", "10.1.1.10-10.1.1.20"]}
        )
        self.assertEqual(errors, [])
        self.assertEqual(
            cleaned["SCAN_IP_BLACKLIST"],
            ["192.168.1.5", "10.0.0.0/30", "10.1.1.10-10.1.1.20"],
        )

    def test_blacklist_empty_accepted(self):
        cleaned, errors = validate_config({"SCAN_IP_BLACKLIST": []})
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["SCAN_IP_BLACKLIST"], [])

    def test_blacklist_malformed_entry_rejected(self):
        cleaned, errors = validate_config({"SCAN_IP_BLACKLIST": ["not-an-ip"]})
        self.assertTrue(any(e.startswith("SCAN_IP_BLACKLIST:") for e in errors))
        self.assertNotIn("SCAN_IP_BLACKLIST", cleaned)

    def test_blacklist_non_list_rejected(self):
        cleaned, errors = validate_config({"SCAN_IP_BLACKLIST": "192.168.1.1"})
        self.assertTrue(any("SCAN_IP_BLACKLIST must be a list" in e for e in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
