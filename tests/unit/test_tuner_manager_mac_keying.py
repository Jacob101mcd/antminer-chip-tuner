"""A9: TunerManager re-keyed by MAC + refresh_engine_ip + iterate MINER_CONFIGS.

Verifies:
- TunerManager.engines is keyed by canonical MAC (or synth ID), not IP.
- get_engine / pop_engine / peek_engine / reset_engine accept either an IP
  or a MAC and resolve internally via canonical_miner_key.
- IP→MAC reverse-lookup uses MINER_CONFIGS[mac]["ip"] field; legacy v3
  fallback returns the identifier unchanged when no v4 entry matches.
- refresh_engine_ip(mac, new_ip) updates engine.ip and rebinds engine.api
  WITHOUT teardown — the existing engine instance and tuning thread are
  unaffected.
- get_overview iterates MINER_CONFIGS.keys() (with MINER_IPS fallback for
  legacy test fixtures) and emits row["mac"] alongside row["ip"].
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.manager.tuner_manager import TunerManager

_MAC_A = "aa:bb:cc:dd:ee:01"
_MAC_B = "aa:bb:cc:dd:ee:02"
_IP_A = "192.0.2.50"
_IP_B = "192.0.2.99"


def _v4_entry(ip, firmware="epic"):
    return {
        "ip": ip,
        "current_firmware": firmware,
        "id_synthesized": False,
        "platforms": {firmware: {}},
    }


class TestEnginesDictKeyedByMac(unittest.TestCase):
    def setUp(self):
        apply_defaults()
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"].pop("MINER_IPS", None)

    def _construct_with_stubbed_engine(self, manager, mac):
        """Avoid spinning up a real engine; install a marker MagicMock."""
        marker = MagicMock(name=f"engine-{mac}")
        marker.mac = mac
        with patch("tuner_app.tuning_engine.engine.TuningEngine", return_value=marker):
            engine = manager.get_engine(mac)
        return engine, marker

    def test_get_engine_with_mac_keys_dict_by_mac(self):
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        manager = TunerManager(state.CONFIG)
        _, marker = self._construct_with_stubbed_engine(manager, _MAC_A)
        self.assertIn(_MAC_A, manager.engines)
        self.assertNotIn(_IP_A, manager.engines)
        self.assertIs(manager.engines[_MAC_A], marker)

    def test_get_engine_with_ip_resolves_to_mac_when_v4_entry_exists(self):
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        manager = TunerManager(state.CONFIG)
        # Pre-populate with the marker under MAC
        marker = MagicMock(name="engine-marker")
        manager.engines[_MAC_A] = marker
        # Asking via IP resolves to the same engine (no new construction)
        engine = manager.get_engine(_IP_A)
        self.assertIs(engine, marker)
        # Dict still has only the MAC entry
        self.assertEqual(list(manager.engines.keys()), [_MAC_A])

    def test_get_engine_with_ip_legacy_fallback_when_no_v4_entry(self):
        """No MINER_CONFIGS entry → identifier flows through unchanged so
        the legacy IP-keyed path still works for test fixtures."""
        manager = TunerManager(state.CONFIG)
        marker = MagicMock(name="legacy-engine")
        manager.engines[_IP_A] = marker
        engine = manager.get_engine(_IP_A)
        self.assertIs(engine, marker)

    def test_pop_engine_via_ip_pops_under_mac(self):
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        manager = TunerManager(state.CONFIG)
        marker = MagicMock(name="engine-A")
        manager.engines[_MAC_A] = marker
        popped = manager.pop_engine(_IP_A)
        self.assertIs(popped, marker)
        self.assertNotIn(_MAC_A, manager.engines)

    def test_peek_engine_via_ip_peeks_under_mac(self):
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        manager = TunerManager(state.CONFIG)
        marker = MagicMock(name="engine-A")
        manager.engines[_MAC_A] = marker
        self.assertIs(manager.peek_engine(_IP_A), marker)
        self.assertIs(manager.peek_engine(_MAC_A), marker)


class TestRefreshEngineIp(unittest.TestCase):
    def setUp(self):
        apply_defaults()
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.MINER_CONFIGS.clear()

    def test_refresh_engine_ip_updates_engine_ip(self):
        manager = TunerManager(state.CONFIG)
        engine = MagicMock()
        engine.ip = _IP_A
        engine.firmware_type = "epic"
        engine.config = MagicMock()
        manager.engines[_MAC_A] = engine
        with patch(
            "tuner_app.manager.tuner_manager.MINER_API_REGISTRY",
            {"epic": MagicMock(name="factory")},
        ):
            manager.refresh_engine_ip(_MAC_A, _IP_B)
        self.assertEqual(engine.ip, _IP_B)

    def test_refresh_engine_ip_rebuilds_api_with_new_ip(self):
        manager = TunerManager(state.CONFIG)
        engine = MagicMock()
        engine.ip = _IP_A
        engine.firmware_type = "epic"
        engine.config = MagicMock()
        manager.engines[_MAC_A] = engine
        new_api = MagicMock(name="new-api")
        factory = MagicMock(return_value=new_api)
        with patch("tuner_app.manager.tuner_manager.MINER_API_REGISTRY", {"epic": factory}):
            manager.refresh_engine_ip(_MAC_A, _IP_B)
        # Factory was called with new IP and engine's config
        factory.assert_called_once_with(_IP_B, engine.config)
        # Engine.api now points at the new instance
        self.assertIs(engine.api, new_api)

    def test_refresh_engine_ip_no_op_when_engine_missing(self):
        manager = TunerManager(state.CONFIG)
        # No engine for _MAC_A — refresh should not raise
        manager.refresh_engine_ip(_MAC_A, _IP_B)

    def test_refresh_engine_ip_no_op_when_ip_unchanged(self):
        """No re-binding when the new IP equals the existing engine.ip."""
        manager = TunerManager(state.CONFIG)
        engine = MagicMock()
        engine.ip = _IP_A
        engine.firmware_type = "epic"
        engine.config = MagicMock()
        manager.engines[_MAC_A] = engine
        factory = MagicMock(name="factory")
        with patch("tuner_app.manager.tuner_manager.MINER_API_REGISTRY", {"epic": factory}):
            manager.refresh_engine_ip(_MAC_A, _IP_A)
        # Factory was not invoked — no rebind
        factory.assert_not_called()

    def test_refresh_engine_ip_no_engine_teardown(self):
        """The engine instance is preserved — no destroy/stop/restart."""
        manager = TunerManager(state.CONFIG)
        engine = MagicMock()
        engine.ip = _IP_A
        engine.firmware_type = "epic"
        engine.config = MagicMock()
        manager.engines[_MAC_A] = engine
        with patch("tuner_app.manager.tuner_manager.MINER_API_REGISTRY", {"epic": MagicMock()}):
            manager.refresh_engine_ip(_MAC_A, _IP_B)
        # destroy/stop never called
        engine.destroy.assert_not_called()
        engine.stop.assert_not_called()
        # Engine still in registry
        self.assertIs(manager.engines[_MAC_A], engine)


class TestGetOverviewIteratesMinerConfigs(unittest.TestCase):
    """get_overview iterates the canonical MAC-keyed MINER_CONFIGS roster
    and emits row['mac'] alongside the existing row['ip'] field."""

    def setUp(self):
        import copy

        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v

    def _stub_engine_status(self, firmware_type="epic"):
        return {
            "phase": "idle",
            "phase_detail": "",
            "tuned_stats": {},
            "firmware_type": firmware_type,
            "avg_board_temp_c": None,
            "avg_chip_temp_c": None,
            "active_sweep_voltage_mv": None,
            "sweep_voltage_mv": None,
            "tuning_complete": False,
            "engine_busy": False,
            "offline_since_ts": None,
            "last_successful_contact_ts": None,
            "mrr_last_sync": None,
        }

    def _make_stub_engine(self):
        engine = MagicMock()
        engine.get_status.return_value = self._stub_engine_status()
        engine.last_summary = None
        engine._get_profit_display_context.return_value = (0.10, None, 0.0)
        return engine

    def test_overview_emits_mac_field_per_row(self):
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        state.CONFIG["fleet_ops"]["MINER_IPS"] = [_IP_A]
        manager = TunerManager(state.CONFIG)
        manager.engines[_MAC_A] = self._make_stub_engine()
        overview = manager.get_overview()
        self.assertEqual(len(overview["miners"]), 1)
        row = overview["miners"][0]
        self.assertEqual(row["ip"], _IP_A)
        self.assertEqual(row["mac"], _MAC_A)

    def test_overview_iterates_miner_configs_not_miner_ips(self):
        """When MINER_CONFIGS has more entries than MINER_IPS (e.g., a stale
        MINER_IPS list), the iteration source is MINER_CONFIGS so all
        registered miners render."""
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        state.MINER_CONFIGS[_MAC_B] = _v4_entry(_IP_B)
        # MINER_IPS only lists one of them — old fleet roster
        state.CONFIG["fleet_ops"]["MINER_IPS"] = [_IP_A]
        manager = TunerManager(state.CONFIG)
        manager.engines[_MAC_A] = self._make_stub_engine()
        manager.engines[_MAC_B] = self._make_stub_engine()
        overview = manager.get_overview()
        ips_in_overview = {r["ip"] for r in overview["miners"]}
        self.assertEqual(ips_in_overview, {_IP_A, _IP_B})

    def test_overview_legacy_fallback_for_miner_ips_without_configs(self):
        """Test fixtures populate MINER_IPS without MINER_CONFIGS entries —
        those rows still render via the legacy fallback."""
        state.CONFIG["fleet_ops"]["MINER_IPS"] = [_IP_A]
        manager = TunerManager(state.CONFIG)
        manager.engines[_IP_A] = self._make_stub_engine()
        overview = manager.get_overview()
        self.assertEqual(len(overview["miners"]), 1)
        row = overview["miners"][0]
        self.assertEqual(row["ip"], _IP_A)
        # Legacy fallback (post-Unit 8 of bulk-regression fix): mac field is
        # converted to a synth-encoded-IP form (`syn-<dashed-ip>`) so the
        # frontend's POST body survives the backend's _normalize_mac validator.
        # The original IPv4 stays in the `ip` field for the UI link.
        self.assertEqual(row["mac"], f"syn-{_IP_A.replace('.', '-')}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
