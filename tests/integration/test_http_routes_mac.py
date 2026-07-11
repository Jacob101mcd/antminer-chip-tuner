"""Integration tests for the MAC-keyed HTTP routes (A12).

Covers behaviors that are unique to the v4 hard cutover:

- URL path validation: dashed MAC, colon MAC (URL-encoded), bare 12-hex,
  synth ID, and rejection of malformed segments via ``MAC_PATH_RE``.
- Body field translation: ``mac`` accepted, ``ip`` rejected with HTTP 400.
- Bulk body translation: ``macs`` accepted, ``ips`` rejected.
- /tuner/miners/set_mac re-keys MINER_CONFIGS, refuses self-rekey, refuses
  conflict with an existing target MAC, refuses unknown source MAC.
- /tuner/config/miner/{mac} URL accepts MAC, body keys land in the v4
  shape (cross-platform top-level for whitelisted keys, platform bucket
  for tuning keys).
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock
from urllib import error, parse, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.config.defaults import apply_defaults
from tuner_app.constants import _mac_for_filename
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server


class _StubManager:
    """Manager stub that records what identifiers it receives."""

    def __init__(self):
        self.engines = {}
        self._lock = threading.RLock()
        self.start_calls = []
        self.stop_calls = []
        self.popped = []
        self.peeked = []

    def get_engine(self, identifier):
        if identifier not in self.engines:
            new = MagicMock(name=f"engine-{identifier}")
            new.mac = identifier
            new.last_summary = None
            new.thread = None
            new.log_lines = []
            self.engines[identifier] = new
        return self.engines[identifier]

    def peek_engine(self, identifier):
        self.peeked.append(identifier)
        return self.engines.get(identifier)

    def pop_engine(self, identifier):
        self.popped.append(identifier)
        return self.engines.pop(identifier, None)

    def start_tuning(self, identifier):
        self.start_calls.append(identifier)
        return True

    def stop_tuning(self, identifier):
        self.stop_calls.append(identifier)


class _RouteTestBase(unittest.TestCase):
    """Common HTTP boot/teardown used by every test class below."""

    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.MINER_CONFIGS.clear()
        apply_defaults()

        import tuner_app.config.persistence as _p

        self._orig_save = _p.save_config_to_disk
        _p.save_config_to_disk = lambda: None

        from tuner_app.http_server.handlers import miners_routes as _mr

        self._orig_mr_save = _mr.save_config_to_disk
        _mr.save_config_to_disk = lambda: None

        from tuner_app.http_server.handlers import bulk_routes as _br

        self._orig_br_save = _br.save_config_to_disk
        _br.save_config_to_disk = lambda: None

        self._stub = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"
        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        import tuner_app.config.persistence as _p

        _p.save_config_to_disk = self._orig_save

        from tuner_app.http_server.handlers import miners_routes as _mr

        _mr.save_config_to_disk = self._orig_mr_save

        from tuner_app.http_server.handlers import bulk_routes as _br

        _br.save_config_to_disk = self._orig_br_save

        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.MINER_CONFIGS.clear()

    def _post(self, path, body, cookie=None):
        req = request.Request(self.base + path, method="POST")
        req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        data = json.dumps(body).encode()
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except error.HTTPError as ex:
            try:
                return ex.code, json.loads(ex.read().decode())
            except Exception:
                return ex.code, {}

    def _get(self, path, cookie=None):
        req = request.Request(self.base + path, method="GET")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            resp = request.urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except error.HTTPError as ex:
            try:
                return ex.code, json.loads(ex.read().decode())
            except Exception:
                return ex.code, {}


class TestMacBodyParsing(_RouteTestBase):
    """``mac`` body field is accepted; ``ip`` is rejected; bad MACs 400."""

    def test_mac_accepts_colon_form(self):
        status, _ = self._post("/tuner/start", {"mac": "AA:BB:CC:DD:EE:FF"}, cookie=self.cookie)
        self.assertEqual(status, 200)
        # Normalized to lowercase colon form.
        self.assertEqual(self._stub.start_calls, ["aa:bb:cc:dd:ee:ff"])

    def test_mac_accepts_dash_form(self):
        status, _ = self._post("/tuner/start", {"mac": "aa-bb-cc-dd-ee-ff"}, cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self._stub.start_calls, ["aa:bb:cc:dd:ee:ff"])

    def test_mac_accepts_bare_hex(self):
        status, _ = self._post("/tuner/start", {"mac": "aabbccddeeff"}, cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self._stub.start_calls, ["aa:bb:cc:dd:ee:ff"])

    def test_mac_accepts_synth_id(self):
        synth = "syn-192-0-2-122-deadbeef"
        status, _ = self._post("/tuner/start", {"mac": synth}, cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self._stub.start_calls, [synth])

    def test_legacy_ip_body_rejected(self):
        status, data = self._post("/tuner/start", {"ip": "10.0.0.1"}, cookie=self.cookie)
        self.assertEqual(status, 400)
        self.assertIn("'ip' body field is no longer accepted", data["error"])
        self.assertEqual(self._stub.start_calls, [])

    def test_invalid_mac_rejected(self):
        for bad in ("xx:yy:zz:11:22:33", "11:22:33:44", "not-a-mac", "aa:bb:cc"):
            status, data = self._post("/tuner/start", {"mac": bad}, cookie=self.cookie)
            self.assertEqual(status, 400, f"input {bad!r} should 400")
            self.assertIn("invalid MAC", data["error"])

    def test_missing_mac_returns_400(self):
        status, data = self._post("/tuner/start", {}, cookie=self.cookie)
        self.assertEqual(status, 400)
        self.assertIn("MAC required", data["error"])


class TestMacsBulkBody(_RouteTestBase):
    """``macs`` array body field is accepted; ``ips`` rejected."""

    def test_macs_accepted_and_normalized(self):
        status, data = self._post(
            "/tuner/bulk/start",
            {"macs": ["AA:BB:CC:11:22:33", "aa-bb-cc-11-22-44"]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        # Normalized lowercase colon form for both, regardless of input form.
        self.assertEqual(
            sorted(self._stub.start_calls),
            ["aa:bb:cc:11:22:33", "aa:bb:cc:11:22:44"],
        )
        self.assertEqual(data["summary"]["succeeded"], 2)

    def test_legacy_ips_array_rejected(self):
        status, data = self._post(
            "/tuner/bulk/start",
            {"ips": ["10.0.0.1", "10.0.0.2"]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertTrue(any("'ips' body field is no longer accepted" in e for e in data["errors"]))
        self.assertEqual(self._stub.start_calls, [])

    def test_invalid_mac_in_macs_rejected(self):
        status, data = self._post(
            "/tuner/bulk/start",
            {"macs": ["aa:bb:cc:dd:ee:ff", "garbage"]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertTrue(any("invalid MAC" in e for e in data["errors"]))


class TestMacPathValidation(_RouteTestBase):
    """URL path segments validated via MAC_PATH_RE."""

    def setUp(self):
        super().setUp()
        # Seed a v4 entry so the engine init in get_engine doesn't blow up
        # when called by status routes. Tests that exercise specific path
        # parsing don't need the engine to do anything meaningful.
        self.mac = "aa:bb:cc:dd:ee:ff"
        state.MINER_CONFIGS[self.mac] = {
            "ip": "10.0.0.1",
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
        }

    def test_live_endpoint_accepts_dash_mac(self):
        # The stub manager's MagicMock engine will respond on get_live_data().
        status, _ = self._get(f"/tuner/live/{_mac_for_filename(self.mac)}", cookie=self.cookie)
        self.assertEqual(status, 200)

    def test_live_endpoint_accepts_colon_mac_url_encoded(self):
        encoded = parse.quote(self.mac, safe="")
        status, _ = self._get(f"/tuner/live/{encoded}", cookie=self.cookie)
        self.assertEqual(status, 200)

    def test_live_endpoint_rejects_malformed_path(self):
        status, _ = self._get("/tuner/live/not-a-mac", cookie=self.cookie)
        self.assertEqual(status, 400)

    def test_live_endpoint_rejects_path_traversal(self):
        status, _ = self._get(
            "/tuner/live/" + parse.quote("../../etc/passwd", safe=""),
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)

    def test_config_miner_endpoint_accepts_dash_mac(self):
        status, data = self._post(
            f"/tuner/config/miner/{_mac_for_filename(self.mac)}",
            {"hostname": "my-miner"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        # Cross-platform key lands at the top level of the v4 entry.
        self.assertEqual(state.MINER_CONFIGS[self.mac]["hostname"], "my-miner")


class TestSetMacEndpoint(_RouteTestBase):
    """/tuner/miners/set_mac re-keys a synth-ID-registered miner."""

    def test_set_mac_rekeys_entry(self):
        synth = "syn-192-0-2-122-deadbeef"
        new_mac = "aa:bb:cc:dd:ee:01"
        state.MINER_CONFIGS[synth] = {
            "ip": "192.0.2.122",
            "current_firmware": "luxos",
            "id_synthesized": True,
            "platforms": {"luxos": {"VOLTAGE_MV": 13500}},
        }
        status, data = self._post(
            "/tuner/miners/set_mac",
            {"old_mac": synth, "new_mac": new_mac},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["new_mac"], new_mac)
        # Entry moved to the new key; flagged as no longer synthesized.
        self.assertNotIn(synth, state.MINER_CONFIGS)
        self.assertIn(new_mac, state.MINER_CONFIGS)
        moved = state.MINER_CONFIGS[new_mac]
        self.assertFalse(moved["id_synthesized"])
        self.assertEqual(moved["current_firmware"], "luxos")
        self.assertEqual(moved["platforms"]["luxos"]["VOLTAGE_MV"], 13500)

    def test_set_mac_refuses_self_rekey(self):
        mac = "aa:bb:cc:dd:ee:11"
        state.MINER_CONFIGS[mac] = {
            "ip": "10.0.0.1",
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
        }
        status, data = self._post(
            "/tuner/miners/set_mac",
            {"old_mac": mac, "new_mac": mac},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("identical", data["error"])

    def test_set_mac_refuses_target_conflict(self):
        old_mac = "syn-1-2-3-4-cafebabe"
        new_mac = "aa:bb:cc:dd:ee:21"
        state.MINER_CONFIGS[old_mac] = {
            "ip": "10.0.0.1",
            "current_firmware": "epic",
            "id_synthesized": True,
            "platforms": {"epic": {}},
        }
        state.MINER_CONFIGS[new_mac] = {
            "ip": "10.0.0.2",
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
        }
        status, data = self._post(
            "/tuner/miners/set_mac",
            {"old_mac": old_mac, "new_mac": new_mac},
            cookie=self.cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("already exists", data["error"])
        # Neither entry moved.
        self.assertIn(old_mac, state.MINER_CONFIGS)
        self.assertIn(new_mac, state.MINER_CONFIGS)

    def test_set_mac_refuses_unknown_source(self):
        status, data = self._post(
            "/tuner/miners/set_mac",
            {"old_mac": "aa:bb:cc:dd:ee:31", "new_mac": "aa:bb:cc:dd:ee:32"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 404)
        self.assertIn("unknown miner", data["error"])

    def test_set_mac_rejects_malformed_input(self):
        status, data = self._post(
            "/tuner/miners/set_mac",
            {"old_mac": "garbage", "new_mac": "aa:bb:cc:dd:ee:41"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)


class TestConfigMinerPathV4Shape(_RouteTestBase):
    """POST /tuner/config/miner/{mac} writes land in the right v4 slots."""

    def setUp(self):
        super().setUp()
        self.mac = "aa:bb:cc:dd:ee:ff"
        state.MINER_CONFIGS[self.mac] = {
            "ip": "10.0.0.1",
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
        }

    def _path(self):
        return f"/tuner/config/miner/{_mac_for_filename(self.mac)}"

    def test_cross_platform_key_lands_at_top_level(self):
        status, data = self._post(
            self._path(),
            {"PASSWORD": "letmein2"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        self.assertEqual(state.MINER_CONFIGS[self.mac]["PASSWORD"], "letmein2")
        # Should NOT have leaked into the platforms bucket.
        self.assertNotIn("PASSWORD", state.MINER_CONFIGS[self.mac]["platforms"]["epic"])

    def test_per_platform_tuning_key_lands_in_platforms_bucket(self):
        status, data = self._post(
            self._path(),
            {"CHIP_FREQ_SPREAD_MHZ": 50},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        self.assertEqual(
            state.MINER_CONFIGS[self.mac]["platforms"]["epic"]["CHIP_FREQ_SPREAD_MHZ"],
            50,
        )
        # Top-level remains clean.
        self.assertNotIn("CHIP_FREQ_SPREAD_MHZ", state.MINER_CONFIGS[self.mac])

    def test_null_drops_per_platform_override(self):
        # Pre-seed an override so we can verify deletion.
        state.MINER_CONFIGS[self.mac]["platforms"]["epic"]["CHIP_FREQ_SPREAD_MHZ"] = 50
        status, data = self._post(
            self._path(),
            {"CHIP_FREQ_SPREAD_MHZ": None},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        self.assertNotIn(
            "CHIP_FREQ_SPREAD_MHZ",
            state.MINER_CONFIGS[self.mac]["platforms"]["epic"],
        )

    def test_null_drops_cross_platform_override(self):
        state.MINER_CONFIGS[self.mac]["PASSWORD"] = "letmein"
        status, _ = self._post(
            self._path(),
            {"PASSWORD": None},
            cookie=self.cookie,
        )
        self.assertNotIn("PASSWORD", state.MINER_CONFIGS[self.mac])

    def test_fleet_only_key_rejected_on_per_miner(self):
        status, data = self._post(
            self._path(),
            {"MRR_API_KEY": "should-not-take"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertTrue(any("fleet-wide" in e for e in data["errors"]))


if __name__ == "__main__":
    unittest.main()
