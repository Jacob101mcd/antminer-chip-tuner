"""Integration tests for scanner HTTP routes.

Covers:
- GET /tuner/scanner/status returns expected JSON shape
- POST /tuner/scanner/scan_now returns {"ok": true} and calls scanner.request_scan_now()
- POST /tuner/scanner/stop returns {"ok": true} and calls scanner.stop()
- All three routes return 401 without an auth session cookie
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.handlers import scanner_routes as _scanner_routes_mod
from tuner_app.http_server.server import start_http_server


class _StubManager:
    def __init__(self):
        self.engines = {}

    def get_all_status(self):
        return []

    def get_overview(self):
        return {"miners": [], "state_counts": {}, "mining_counts": {}}


class TestScannerRoutes(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )

        # Create a mock scanner and inject into scanner_routes module
        self._mock_scanner = MagicMock()
        self._mock_scanner.get_status.return_value = {
            "state": "idle",
            "last_run_started_at": None,
            "last_run_finished_at": None,
            "progress": 0,
            "discovered": [],
            "errors": [],
        }
        self._orig_scanner = _scanner_routes_mod.scanner
        _scanner_routes_mod.scanner = self._mock_scanner

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
        _scanner_routes_mod.scanner = self._orig_scanner
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})

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

    # ── status endpoint ──────────────────────────────────────────────────────

    def test_scanner_status_returns_json_shape(self):
        status, body = self._request("GET", "/tuner/scanner/status", cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("state", data)
        self.assertIn("discovered", data)
        self.assertIn("errors", data)
        self._mock_scanner.get_status.assert_called_once()

    def test_scanner_status_requires_auth(self):
        status, _ = self._request("GET", "/tuner/scanner/status")
        self.assertEqual(status, 401)

    # ── scan_now endpoint ────────────────────────────────────────────────────

    def test_scanner_scan_now_returns_ok(self):
        status, body = self._request("POST", "/tuner/scanner/scan_now", body={}, cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self._mock_scanner.request_scan_now.assert_called_once()

    def test_scanner_scan_now_requires_auth(self):
        status, _ = self._request("POST", "/tuner/scanner/scan_now", body={})
        self.assertEqual(status, 401)

    # ── stop endpoint ────────────────────────────────────────────────────────

    def test_scanner_stop_returns_ok(self):
        status, body = self._request("POST", "/tuner/scanner/stop", body={}, cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self._mock_scanner.stop.assert_called_once()

    def test_scanner_stop_requires_auth(self):
        status, _ = self._request("POST", "/tuner/scanner/stop", body={})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
