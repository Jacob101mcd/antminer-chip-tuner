"""Integration tests for the engine-control route handlers.

Covers:
- /tuner/start with no MAC returns 400
- /tuner/start with MAC returns 200 + result of manager.start_tuning()
- /tuner/stop with no MAC returns 400
- /tuner/delete_profile validates scope and dispatches to bulk._delete_profile_for_ip
- Legacy {ip:...} body shape is rejected with HTTP 400
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server

_SAMPLE_MAC = "aa:bb:cc:dd:ee:ff"


class _StubManager:
    """Manager stub recording start/stop calls."""

    def __init__(self):
        self.engines = {}
        self.started_keys = []
        self.stopped_keys = []

    def start_tuning(self, identifier):
        self.started_keys.append(identifier)
        return True

    def stop_tuning(self, identifier):
        self.stopped_keys.append(identifier)


class TestControlRoutes(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )

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

    def test_start_without_mac_returns_400(self):
        status, body = self._request("POST", "/tuner/start", {}, cookie=self.cookie)
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["started"])
        self.assertIn("MAC required", data["error"])

    def test_start_with_mac_dispatches_to_manager(self):
        status, body = self._request(
            "POST", "/tuner/start", {"mac": _SAMPLE_MAC}, cookie=self.cookie
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["started"])
        self.assertEqual(self._stub_manager.started_keys, [_SAMPLE_MAC])

    def test_start_with_legacy_ip_body_returns_400(self):
        """Hard cutover: the old {ip:...} body shape is rejected with HTTP 400."""
        status, body = self._request(
            "POST", "/tuner/start", {"ip": "192.168.1.1"}, cookie=self.cookie
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["started"])
        self.assertIn("'ip' body field is no longer accepted", data["error"])
        self.assertEqual(self._stub_manager.started_keys, [])

    def test_start_with_invalid_mac_returns_400(self):
        status, body = self._request(
            "POST", "/tuner/start", {"mac": "not-a-mac"}, cookie=self.cookie
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["started"])
        self.assertIn("invalid MAC", data["error"])

    def test_stop_without_mac_returns_400(self):
        status, body = self._request("POST", "/tuner/stop", {}, cookie=self.cookie)
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["stopped"])
        self.assertIn("MAC required", data["error"])

    def test_stop_with_mac_dispatches_to_manager(self):
        status, body = self._request(
            "POST", "/tuner/stop", {"mac": _SAMPLE_MAC}, cookie=self.cookie
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["stopped"])
        self.assertEqual(self._stub_manager.stopped_keys, [_SAMPLE_MAC])

    def test_delete_profile_invalid_scope_returns_400(self):
        with patch(
            "tuner_app.http_server.handlers.control_routes._delete_profile_for_ip"
        ) as mock_del:
            status, body = self._request(
                "POST",
                "/tuner/delete_profile",
                {"mac": _SAMPLE_MAC, "scope": "bogus_scope"},
                cookie=self.cookie,
            )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["deleted"])
        self.assertIn("invalid scope", data["error"])
        mock_del.assert_not_called()

    def test_delete_profile_valid_scope_dispatches(self):
        with patch(
            "tuner_app.http_server.handlers.control_routes._delete_profile_for_ip"
        ) as mock_del:
            status, body = self._request(
                "POST",
                "/tuner/delete_profile",
                {"mac": _SAMPLE_MAC, "scope": "all"},
                cookie=self.cookie,
            )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["deleted"])
        self.assertEqual(data["scope"], "all")
        mock_del.assert_called_once_with(_SAMPLE_MAC, scope="all")

    def test_delete_profile_no_mac_returns_400(self):
        status, body = self._request(
            "POST", "/tuner/delete_profile", {"scope": "all"}, cookie=self.cookie
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["deleted"])
        self.assertIn("MAC required", data["error"])


if __name__ == "__main__":
    unittest.main()
