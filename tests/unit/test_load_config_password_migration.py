"""Unit tests for PASSWORD->SCAN_PASSWORDS migration in tuner_app.config.persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch

from tuner_app import state
from tuner_app.config import persistence
from tuner_app.config.defaults import apply_defaults


class TestPasswordScanPasswordsMigration(unittest.TestCase):
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

    def test_password_migrated_to_scan_passwords(self):
        """PASSWORD from a pre-migration config is prepended to SCAN_PASSWORDS."""
        self._load_with_defaults({"PASSWORD": "secret", "SCAN_PASSWORDS": ["letmein"]})
        fo = state.CONFIG["fleet_ops"]
        self.assertEqual(fo["SCAN_PASSWORDS"][0], "secret")
        self.assertIn("secret", fo["SCAN_PASSWORDS"])
        self.assertIn("letmein", fo["SCAN_PASSWORDS"])
        self.assertEqual(fo["PASSWORD"], "secret")

    def test_migration_idempotent(self):
        """Config with no PASSWORD key is a no-op — SCAN_PASSWORDS unchanged."""
        self._load_with_defaults({"SCAN_PASSWORDS": ["secret", "letmein"]})
        fo = state.CONFIG["fleet_ops"]
        self.assertEqual(fo["SCAN_PASSWORDS"], ["secret", "letmein"])
        # PASSWORD was not in the disk config, so it stays at the apply_defaults value.
        self.assertEqual(fo["PASSWORD"], "letmein")

    def test_password_deduped_if_already_in_scan_passwords(self):
        """PASSWORD already at SCAN_PASSWORDS[0] produces no duplicate entry."""
        self._load_with_defaults({"PASSWORD": "letmein", "SCAN_PASSWORDS": ["letmein", "other"]})
        fo = state.CONFIG["fleet_ops"]
        self.assertEqual(fo["SCAN_PASSWORDS"], ["letmein", "other"])
        self.assertEqual(fo["SCAN_PASSWORDS"].count("letmein"), 1)

    def test_per_miner_password_override_preserved(self):
        """Per-miner PASSWORD in MINER_CONFIGS is NOT purged by the orphan-cleanup OR by the
        v3→v4 migration. Post-self-heal, the entry is re-keyed from IP to a synth ID (because
        EpicMinerAPI / ARP both fail in this test environment), but PASSWORD survives the move."""
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ["192.168.1.1"]
        self._load_with_defaults(
            {"SCAN_PASSWORDS": ["letmein"]},
            miner_configs={"192.168.1.1": {"PASSWORD": "minerpass"}},
        )
        # Old IP key is gone; new MAC-or-synth key exists with the password preserved.
        self.assertNotIn("192.168.1.1", state.MINER_CONFIGS)
        # Find the migrated entry by its `ip` field.
        matching = [
            (k, v)
            for k, v in state.MINER_CONFIGS.items()
            if isinstance(v, dict) and v.get("ip") == "192.168.1.1"
        ]
        self.assertEqual(len(matching), 1)
        _key, entry = matching[0]
        self.assertEqual(entry["PASSWORD"], "minerpass")

    def test_config_fleet_ops_handler_syncs_password_from_scan_passwords(self):
        """POST /tuner/config/fleet_ops with SCAN_PASSWORDS must update CONFIG[PASSWORD]
        in-memory so the engine MinerAPI auth fallback (engine.py:86) picks up the
        new value without a process restart.

        SCAN_PASSWORDS is a fleet-ops key — it goes to /tuner/config/fleet_ops, not
        /tuner/config/defaults (which only accepts per-platform tuning keys).
        """
        from tuner_app.http_server.handlers import miners_routes

        state.CONFIG["fleet_ops"]["SCAN_PASSWORDS"] = ["letmein"]
        state.CONFIG["fleet_ops"]["PASSWORD"] = "letmein"

        class _StubHandler:
            def __init__(self):
                self.responses = []
                self.manager = type("M", (), {"engines": {}})()

            def _json_response(self, payload, status=200):
                self.responses.append((status, payload))

        handler = _StubHandler()
        body = '{"SCAN_PASSWORDS": ["newpass", "letmein"]}'
        with patch("tuner_app.http_server.handlers.miners_routes.save_config_to_disk"):
            miners_routes.config_fleet_ops(handler, body)
        fo = state.CONFIG["fleet_ops"]
        self.assertEqual(fo["SCAN_PASSWORDS"], ["newpass", "letmein"])
        self.assertEqual(fo["PASSWORD"], "newpass")
        self.assertEqual(handler.responses[-1][1].get("updated"), True)
