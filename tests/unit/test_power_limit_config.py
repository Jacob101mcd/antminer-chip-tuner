"""Unit tests for POWER_LIMIT_W config knob and Bixbit persistence invariants.

Covers:
- POWER_LIMIT_W present in CONFIG_DEFAULTS (3500), CONFIG_BOUNDS (1500-6000), FLEET_ONLY_KEYS
- validate_config bounds enforcement for POWER_LIMIT_W
- Migration: config.json missing POWER_LIMIT_W loads with default 3500
- Bixbit persistence: empty per-board chip arrays (e.g. [[], [], []]) are truthy
  and survive the `saved.get(key) or _empty_board_arrays()` load pattern as-is
"""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from tuner_app import state
from tuner_app.config import persistence
from tuner_app.config.defaults import CONFIG_DEFAULTS, apply_defaults
from tuner_app.config.schema import CONFIG_BOUNDS
from tuner_app.config.validation import validate_config
from tuner_app.constants import FLEET_ONLY_KEYS


class TestPowerLimitConfigSchema(unittest.TestCase):
    def test_power_limit_w_in_config_defaults(self):
        """POWER_LIMIT_W is in CONFIG_DEFAULTS with value 3500."""
        self.assertEqual(CONFIG_DEFAULTS["POWER_LIMIT_W"], 3500)

    def test_power_limit_w_in_config_bounds(self):
        """POWER_LIMIT_W is in CONFIG_BOUNDS with range (1500, 6000)."""
        self.assertEqual(CONFIG_BOUNDS["POWER_LIMIT_W"], (1500, 6000))

    def test_power_limit_w_not_in_fleet_only_keys(self):
        """POWER_LIMIT_W is a per-platform key in v3 (not a fleet_ops singleton).
        Per-miner overrides are still disallowed at the HTTP layer via platform-
        bucket semantics, but the key is no longer in FLEET_OPS_KEYS."""
        self.assertNotIn("POWER_LIMIT_W", FLEET_ONLY_KEYS)


class TestPowerLimitValidation(unittest.TestCase):
    def test_power_limit_w_valid_default(self):
        """validate_config accepts POWER_LIMIT_W=3500 with no errors."""
        _cleaned, errors = validate_config({"POWER_LIMIT_W": 3500})
        self.assertEqual(errors, [])

    def test_power_limit_w_below_bound_rejected(self):
        """validate_config rejects POWER_LIMIT_W=1499 (below lower bound 1500)."""
        _cleaned, errors = validate_config({"POWER_LIMIT_W": 1499})
        self.assertTrue(any("POWER_LIMIT_W" in e for e in errors))

    def test_power_limit_w_above_bound_rejected(self):
        """validate_config rejects POWER_LIMIT_W=6001 (above upper bound 6000)."""
        _cleaned, errors = validate_config({"POWER_LIMIT_W": 6001})
        self.assertTrue(any("POWER_LIMIT_W" in e for e in errors))


class TestPowerLimitPersistence(unittest.TestCase):
    def setUp(self):
        import copy

        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov

    def _load_with_defaults(self, defaults_data, miner_configs=None):
        """Write a v2 config file and call load_config_from_disk."""
        payload = {
            "version": 2,
            "defaults": defaults_data,
            "miner_configs": miner_configs or {},
            "auth": {},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(payload, f)
            config_file = f.name
        with patch("tuner_app.config.persistence.CONFIG_FILE", config_file):
            persistence.load_config_from_disk()

    def test_migration_absent_power_limit_w_defaults_to_3500(self):
        """Config missing POWER_LIMIT_W loads cleanly; platform defaults to 3500."""
        self._load_with_defaults({})
        # POWER_LIMIT_W is a per-platform key — check any platform bucket
        self.assertEqual(state.CONFIG["defaults"]["epic"]["POWER_LIMIT_W"], 3500)

    def test_bixbit_empty_board_arrays_truthy(self):
        """[[], [], []] (Bixbit empty per-board arrays) is truthy — survives `or` in load path.

        The persistence load pattern is `saved.get(key) or engine._empty_board_arrays()`.
        For Bixbit miners, per-chip arrays are [[], [], []] (one empty list per board).
        A non-empty outer list is truthy even when inner lists are empty, so `or` does NOT
        fall through to _empty_board_arrays() — Bixbit's empty arrays load as-is.
        """
        saved = {"baseline_chip_temps": [[], [], []]}
        fallback = "should_not_be_returned"
        result = saved.get("baseline_chip_temps") or fallback
        self.assertEqual(result, [[], [], []])

    def test_missing_key_falls_through_to_fallback(self):
        """Missing key returns None from saved.get(), so `or fallback` fires correctly.

        When a key is absent from the saved dict, `saved.get(key)` returns None (falsy),
        so `None or fallback` returns the fallback — triggering _empty_board_arrays().
        """
        saved = {}
        fallback = "fallback_value"
        result = saved.get("baseline_chip_temps") or fallback
        self.assertEqual(result, "fallback_value")

    def test_empty_list_falls_through_to_fallback(self):
        """An empty outer list [] (fully absent chip data) is falsy — falls to fallback.

        This edge case ([] vs [[], [], []]) is safe: [] means no per-chip data at all,
        so falling through to _empty_board_arrays() correctly re-initializes the shape.
        """
        saved = {"baseline_chip_temps": []}
        fallback = "fallback_value"
        result = saved.get("baseline_chip_temps") or fallback
        self.assertEqual(result, "fallback_value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
