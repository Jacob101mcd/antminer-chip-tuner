"""Tests for firmware_type field: validation, migration, and _MINER_CONFIG_DEFAULTS."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tuner_app import state
from tuner_app.config.defaults import _MINER_CONFIG_DEFAULTS
from tuner_app.config.validation import validate_config


class TestMinerConfigDefaults(unittest.TestCase):
    def test_miner_config_defaults_has_firmware_type(self):
        self.assertIn("firmware_type", _MINER_CONFIG_DEFAULTS)
        self.assertEqual(_MINER_CONFIG_DEFAULTS["firmware_type"], "epic")


class TestFirmwareTypeValidation(unittest.TestCase):
    def test_epic_accepted(self):
        cleaned, errors = validate_config({"firmware_type": "epic"})
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["firmware_type"], "epic")

    def test_bixbit_accepted(self):
        cleaned, errors = validate_config({"firmware_type": "bixbit"})
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["firmware_type"], "bixbit")

    def test_case_insensitive(self):
        cleaned, errors = validate_config({"firmware_type": "EPIC"})
        self.assertEqual(errors, [])
        self.assertEqual(cleaned["firmware_type"], "epic")

    def test_invalid_value_rejected(self):
        _cleaned, errors = validate_config({"firmware_type": "luxor"})
        self.assertTrue(any("firmware_type" in e for e in errors))

    def test_non_string_rejected(self):
        _cleaned, errors = validate_config({"firmware_type": 42})
        self.assertTrue(any("firmware_type" in e for e in errors))

    def test_empty_string_rejected(self):
        _cleaned, errors = validate_config({"firmware_type": ""})
        self.assertTrue(any("firmware_type" in e for e in errors))


class TestFirmwareTypeMigration(unittest.TestCase):
    """Verify load_config_from_disk migrates legacy v2/v3 firmware_type to v4.

    The v3→v4 migration (PR2) re-keys MINER_CONFIGS by MAC and renames
    ``firmware_type`` → ``current_firmware`` at the top level. These tests
    patch DATA_DIR per call so the sentinel file (``.migration_v3_to_v4.done``)
    lives in a temp dir and the migration always fires — without DATA_DIR
    patching, the project-root sentinel would silently skip the migration
    after the first test run and leave the legacy ``firmware_type`` key in
    place, masking real regressions.
    """

    def setUp(self):
        # Save original state
        self._orig_miner_ips = list(state.CONFIG["fleet_ops"].get("MINER_IPS", []))
        self._orig_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}

    def tearDown(self):
        state.CONFIG["fleet_ops"]["MINER_IPS"] = self._orig_miner_ips
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS.update(self._orig_miner_configs)

    def _run_load_with_config(self, config_dict, miner_ips):
        """Helper: write config_dict to a tmp file, patch DATA_DIR + CONFIG_FILE,
        stub resolve_mac to None so the synth-id path is exercised, call
        load_config_from_disk. Returns the synth MAC (deterministic) for
        post-migration assertions.
        """
        import contextlib
        import shutil
        from unittest.mock import patch

        from tuner_app.config.persistence import load_config_from_disk

        state.CONFIG["fleet_ops"]["MINER_IPS"] = list(miner_ips)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config_dict, f)
            tmp_path = f.name
        tmp_data_dir = tempfile.mkdtemp()

        try:
            # ExitStack avoids parenthesized-with syntax (py3.10+) so this
            # file stays parseable under the project's py38 ruff target.
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", tmp_path))
                stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", tmp_data_dir))
                stack.enter_context(patch("tuner_app.constants.DATA_DIR", tmp_data_dir))
                # Force the synth-id path so the resulting MAC key is
                # deterministic per (ip) regardless of host ARP state.
                stack.enter_context(
                    patch(
                        "tuner_app.config.persistence.resolve_mac",
                        return_value=None,
                    )
                )
                stack.enter_context(
                    patch(
                        "tuner_app.config.persistence.synthesize_mac_id",
                        side_effect=lambda ip: "syn-" + ip.replace(".", "-") + "-deadbeef",
                    )
                )
                load_config_from_disk()
        finally:
            os.unlink(tmp_path)
            shutil.rmtree(tmp_data_dir, ignore_errors=True)

    def _entry_for_ip(self, ip):
        """Find the v4 MINER_CONFIGS entry whose ``ip`` field matches *ip*.

        Used by post-migration assertions: the v3→v4 migration re-keys by
        synth MAC so the entry is no longer at ``MINER_CONFIGS[ip]``.
        """
        for _mac, entry in state.MINER_CONFIGS.items():
            if isinstance(entry, dict) and entry.get("ip") == ip:
                return entry
        return None

    def test_migration_backfills_firmware_type(self):
        """Loading a config.json lacking firmware_type assigns 'epic' on the
        v4 ``current_firmware`` field."""
        old_config = {
            "version": 2,
            "defaults": {"MINER_IPS": ["192.0.2.10"]},
            "miner_configs": {
                "192.0.2.10": {"PASSWORD": "letmein"},
            },
            "auth": {},
        }
        self._run_load_with_config(old_config, ["192.0.2.10"])

        entry = self._entry_for_ip("192.0.2.10")
        self.assertIsNotNone(entry, "v4 entry for 192.0.2.10 must exist post-migration")
        self.assertEqual(
            entry.get("current_firmware"),
            "epic",
            "current_firmware must default to 'epic' for entries lacking firmware_type",
        )
        # PASSWORD is a CROSS_PLATFORM_PER_MINER_KEY → top-level in v4.
        self.assertEqual(entry.get("PASSWORD"), "letmein")

    def test_existing_firmware_type_preserved(self):
        """v3-shape ``firmware_type: bixbit`` migrates to v4 ``current_firmware: bixbit``."""
        old_config = {
            "version": 2,
            "defaults": {"MINER_IPS": ["192.0.2.11"]},
            "miner_configs": {
                "192.0.2.11": {"firmware_type": "bixbit"},
            },
            "auth": {},
        }
        self._run_load_with_config(old_config, ["192.0.2.11"])

        entry = self._entry_for_ip("192.0.2.11")
        self.assertIsNotNone(entry, "v4 entry for 192.0.2.11 must exist post-migration")
        self.assertEqual(
            entry.get("current_firmware"),
            "bixbit",
            "Existing firmware_type must migrate to current_firmware unchanged",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
