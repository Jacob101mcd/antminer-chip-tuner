"""Integration tests for the bulk-action route handlers.

Covers (post-A12 MAC cutover):
- /tuner/bulk/start returns the canonical {results, summary} shape keyed by MAC
- /tuner/bulk/start summary counts succeeded/failed correctly
- /tuner/bulk/apply_config returns 400 + errors when ANY value invalid
  (no partial writes, body shape preserved)
- /tuner/bulk/apply_config writes overrides to MINER_CONFIGS[mac]["platforms"][platform]
  on success
- /tuner/bulk/reset_profile rejects invalid scope with 400
- Legacy {ips:...} body shape is rejected with HTTP 400
"""

from __future__ import annotations

import json
import threading
import unittest
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server

_MAC_A = "aa:bb:cc:dd:ee:01"
_MAC_B = "aa:bb:cc:dd:ee:02"


class _StubManager:
    """Manager stub recording bulk start/stop calls and selectively raising."""

    def __init__(self):
        self.engines = {}
        self.start_returns = {}  # identifier -> bool or Exception
        self.stopped_keys = []

    def start_tuning(self, identifier):
        rv = self.start_returns.get(identifier, True)
        if isinstance(rv, Exception):
            raise rv
        return rv

    def stop_tuning(self, identifier):
        self.stopped_keys.append(identifier)


class TestBulkRoutes(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.MINER_CONFIGS.clear()

        # Patch out save_config_to_disk so the bulk_apply_config doesn't write.
        import tuner_app.config.persistence as persistence_mod

        self._orig_save = persistence_mod.save_config_to_disk
        persistence_mod.save_config_to_disk = lambda: None

        from tuner_app.http_server.handlers import bulk_routes as _br

        self._orig_br_save = _br.save_config_to_disk
        _br.save_config_to_disk = lambda: None

        self._stub_manager = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub_manager)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"

        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        import tuner_app.config.persistence as persistence_mod

        persistence_mod.save_config_to_disk = self._orig_save

        from tuner_app.http_server.handlers import bulk_routes as _br

        _br.save_config_to_disk = self._orig_br_save

        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.MINER_CONFIGS.clear()

    def _request(self, method, path, body=None, cookie=None):
        req = request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, resp.read().decode()
        except error.HTTPError as ex:
            return ex.code, ex.read().decode()

    def test_bulk_start_canonical_shape(self):
        status, body = self._request(
            "POST",
            "/tuner/bulk/start",
            {"macs": [_MAC_A, _MAC_B]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        # Canonical schema-v4 shape.
        self.assertIn("results", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["succeeded"], 2)
        self.assertEqual(data["summary"]["failed"], 0)
        for mac in (_MAC_A, _MAC_B):
            self.assertIn(mac, data["results"])
            self.assertTrue(data["results"][mac]["ok"])
            self.assertIsNone(data["results"][mac]["error"])

    def test_bulk_start_failure_on_one_mac(self):
        self._stub_manager.start_returns[_MAC_B] = RuntimeError("boom")
        status, body = self._request(
            "POST",
            "/tuner/bulk/start",
            {"macs": [_MAC_A, _MAC_B]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["succeeded"], 1)
        self.assertEqual(data["summary"]["failed"], 1)
        self.assertTrue(data["results"][_MAC_A]["ok"])
        self.assertFalse(data["results"][_MAC_B]["ok"])
        self.assertIn("boom", data["results"][_MAC_B]["error"])

    def test_bulk_start_legacy_ips_body_returns_400(self):
        """Hard cutover: the old {ips:...} body shape is rejected with HTTP 400."""
        status, body = self._request(
            "POST",
            "/tuner/bulk/start",
            {"ips": ["10.0.0.1", "10.0.0.2"]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertTrue(any("'ips' body field is no longer accepted" in e for e in data["errors"]))

    def test_bulk_apply_config_invalid_value_returns_400(self):
        # Submit an invalid value (out-of-bounds). Validation rejects whole batch.
        # CHIP_FREQ_SPREAD_MHZ has bounds; 99999 is way out of range.
        status, body = self._request(
            "POST",
            "/tuner/bulk/apply_config",
            {
                "macs": [_MAC_A],
                "platform": "epic",
                "config": {"CHIP_FREQ_SPREAD_MHZ": 99999},
            },
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertEqual(data["summary"]["total"], 0)
        self.assertEqual(data["summary"]["succeeded"], 0)
        self.assertEqual(data["summary"]["failed"], 0)
        self.assertGreater(len(data["errors"]), 0)
        # No partial writes occurred
        self.assertNotIn(_MAC_A, state.MINER_CONFIGS)

    def test_bulk_apply_config_invalid_body_type(self):
        status, body = self._request(
            "POST",
            "/tuner/bulk/apply_config",
            {"macs": [_MAC_A], "platform": "epic", "config": "not_an_object"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertIn("config must be an object", data["errors"])

    def test_bulk_apply_config_writes_to_overrides(self):
        """The override lands inside ``platforms[platform]`` per the v4 schema."""
        # Register the miner as epic first (v4 shape).
        with state.config_lock:
            state.MINER_CONFIGS[_MAC_A] = {
                "ip": "10.0.0.1",
                "current_firmware": "epic",
                "id_synthesized": False,
                "platforms": {"epic": {}},
            }
        status, body = self._request(
            "POST",
            "/tuner/bulk/apply_config",
            {"macs": [_MAC_A], "platform": "epic", "config": {"CHIP_FREQ_SPREAD_MHZ": 50}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["summary"]["succeeded"], 1)
        # Verify the override was written into the platform bucket (v4).
        self.assertIn(_MAC_A, state.MINER_CONFIGS)
        self.assertEqual(
            state.MINER_CONFIGS[_MAC_A]["platforms"]["epic"]["CHIP_FREQ_SPREAD_MHZ"],
            50,
        )

    def test_bulk_apply_config_without_platform_rejected(self):
        """Legacy body (no platform) is rejected with 400."""
        status, body = self._request(
            "POST",
            "/tuner/bulk/apply_config",
            {"macs": [_MAC_A], "config": {"CHIP_FREQ_SPREAD_MHZ": 50}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertTrue(any("'platform' is required" in e for e in data["errors"]))

    def test_bulk_reset_profile_invalid_scope(self):
        status, body = self._request(
            "POST",
            "/tuner/bulk/reset_profile",
            {"macs": [_MAC_A], "scope": "bogus"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertEqual(data["summary"]["total"], 0)
        self.assertIn("invalid scope", data["errors"][0])


if __name__ == "__main__":
    unittest.main()
