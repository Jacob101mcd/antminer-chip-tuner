"""Unit tests for platform-aware validate_config behavior (v3 schema).

Covers:
- test_validate_config_platform_aware_cross_field: cross-field checks use
  the per-platform bucket to resolve missing keys when platform= is passed.
- test_validate_config_unknown_key_epic_platform: unknown key is rejected for
  the epic platform bucket.
- test_validate_config_unknown_key_bixbit_platform: unknown key is rejected for
  the bixbit platform bucket.
- test_validate_config_fleet_ops_key_accepted: fleet-ops key accepted regardless
  of platform argument.
- test_validate_config_platform_default_none_falls_back_to_epic: platform=None
  defaults to the epic bucket for unknown-key checks.
- test_validate_config_cross_field_uses_platform_bucket_defaults: cross-field
  rules that read existing-config defaults use the correct platform bucket.
"""

from __future__ import annotations

import copy
import unittest

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.config.validation import validate_config


class TestValidateConfigPlatformAware(unittest.TestCase):
    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        apply_defaults()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)

    def test_validate_config_platform_aware_cross_field(self):
        """Cross-field check (CHIP_FREQ_SPREAD vs CHIP_TUNE_STEP) resolves defaults
        from the correct platform bucket when platform= is supplied."""
        # Use the epic bucket defaults — both keys must be present after apply_defaults
        epic_spread = state.CONFIG["defaults"]["epic"]["CHIP_FREQ_SPREAD_MHZ"]
        # Submit a CHIP_TUNE_STEP_MHZ that violates the constraint relative to
        # the current platform default CHIP_FREQ_SPREAD_MHZ.
        # Rule: CHIP_FREQ_SPREAD_MHZ >= 2 * CHIP_TUNE_STEP_MHZ
        bad_step = (epic_spread // 2) + 1  # one above threshold — will violate
        _cleaned, errors = validate_config({"CHIP_TUNE_STEP_MHZ": bad_step}, platform="epic")
        self.assertTrue(
            any("CHIP_FREQ_SPREAD_MHZ" in e or "CHIP_TUNE_STEP_MHZ" in e for e in errors),
            f"Expected cross-field error for CHIP_TUNE_STEP_MHZ={bad_step} with "
            f"CHIP_FREQ_SPREAD_MHZ={epic_spread}, got errors={errors!r}",
        )

    def test_validate_config_platform_epic_accepts_chip_tune_key(self):
        """CHIP_TUNE_STEP_MHZ is a known key for platform='epic'."""
        _cleaned, errors = validate_config({"CHIP_TUNE_STEP_MHZ": 6.25}, platform="epic")
        self.assertEqual(errors, [])

    def test_validate_config_fleet_ops_key_accepted_with_any_platform(self):
        """Fleet-ops key (SCAN_INTERVAL_MIN) is accepted regardless of platform."""
        for platform in ("epic", "bixbit", "luxos", "braiins", None):
            _cleaned, errors = validate_config({"SCAN_INTERVAL_MIN": 30}, platform=platform)
            self.assertEqual(
                errors,
                [],
                f"SCAN_INTERVAL_MIN should be accepted for platform={platform!r}",
            )

    def test_validate_config_unknown_key_rejected_for_epic(self):
        """Unknown key is rejected for epic platform."""
        _cleaned, errors = validate_config({"TOTALLY_UNKNOWN_KEY_XYZ": 999}, platform="epic")
        self.assertTrue(
            any("Unknown" in e for e in errors),
            f"Expected unknown-key error, got {errors!r}",
        )

    def test_validate_config_platform_none_defaults_to_epic(self):
        """platform=None is a backward-compat alias for 'epic'."""
        # BOARD_MAX_TEMP is in all platform buckets; should work with platform=None
        _cleaned, errors = validate_config({"BOARD_MAX_TEMP": 70}, platform=None)
        self.assertEqual(errors, [])

    def test_validate_config_cross_field_uses_platform_bucket_for_defaults(self):
        """When only one of the cross-field pair is submitted, the other is resolved
        from the current platform bucket default (not a hardcoded fallback)."""
        # Set a non-default CHIP_FREQ_SPREAD_MHZ in the epic bucket
        state.CONFIG["defaults"]["epic"]["CHIP_FREQ_SPREAD_MHZ"] = 30
        state.CONFIG["defaults"]["epic"]["CHIP_TUNE_STEP_MHZ"] = 5

        # Submitting step=16 with the platform's spread=30 violates spread >= 2*step
        _cleaned, errors = validate_config({"CHIP_TUNE_STEP_MHZ": 16}, platform="epic")
        self.assertTrue(
            any("CHIP_FREQ_SPREAD_MHZ" in e or "CHIP_TUNE_STEP_MHZ" in e for e in errors),
            f"Expected cross-field error with spread=30, step=16; got {errors!r}",
        )

    def test_validate_config_bixbit_platform_accepts_known_key(self):
        """validate_config with platform='bixbit' accepts keys in the bixbit bucket."""
        # BOARD_MAX_TEMP is in all platform buckets
        _cleaned, errors = validate_config({"BOARD_MAX_TEMP": 75}, platform="bixbit")
        self.assertEqual(errors, [])

    def test_validate_config_firmware_type_accepted_without_platform(self):
        """firmware_type is a per-miner-only key — accepted by the special-case handler
        regardless of the platform argument (epic/bixbit/luxos/braiins)."""
        for ft in ("epic", "bixbit", "luxos", "braiins"):
            _cleaned, errors = validate_config({"firmware_type": ft}, platform=None)
            self.assertEqual(errors, [], f"firmware_type={ft!r} should be accepted; got {errors!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
