"""Integration tests for the auth route handlers.

Covers:
- GET /tuner/auth/status returns 200 without a cookie (auth-exempt)
- GET /tuner/status returns 401 without a cookie
- POST /tuner/setup with valid password configures auth and issues cookie
- POST /tuner/setup when already configured returns 400
- POST /tuner/login with wrong password returns 401
- POST /tuner/login with correct password returns 200 + Set-Cookie
- POST /tuner/logout invalidates session
"""

from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest.mock import patch
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.http_server.auth_helpers import TRUSTED_PROXIES_ENV
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server


class _StubManager:
    """Minimal stub. Auth tests only need /tuner/status to be reachable."""

    def __init__(self):
        self.engines = {}

    def get_all_status(self):
        return {}


class TestAuthRoutes(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state._login_attempts.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})

        import tuner_app.config.persistence as persistence_mod

        self._orig_save = persistence_mod.save_config_to_disk
        persistence_mod.save_config_to_disk = lambda: None

        from tuner_app.http_server.handlers import auth_routes as _ar

        self._orig_ar_save = _ar.save_config_to_disk
        _ar.save_config_to_disk = lambda: None

        self._stub_manager = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub_manager)
        port = self.server.server_address[1]
        self.port = port
        self.host_header = f"localhost:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        import tuner_app.config.persistence as persistence_mod

        persistence_mod.save_config_to_disk = self._orig_save

        from tuner_app.http_server.handlers import auth_routes as _ar

        _ar.save_config_to_disk = self._orig_ar_save

        state._sessions.clear()
        state._login_attempts.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})

    def _request(self, method, path, body=None, cookie=None, headers=None):
        """Send an HTTP request, return (status, body_str, set_cookie_header)."""
        req = request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        if cookie:
            req.add_header("Cookie", cookie)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, resp.read().decode(), resp.headers.get("Set-Cookie")
        except error.HTTPError as ex:
            body_text = ex.read().decode()
            sc = ex.headers.get("Set-Cookie") if ex.headers else None
            return ex.code, body_text, sc

    def _raw_post(self, path, headers):
        """Send header lines without http.client merging duplicate fields."""
        conn = http.client.HTTPConnection("localhost", self.port, timeout=5)
        conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers:
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        result = (response.status, response.read().decode())
        conn.close()
        return result

    def test_auth_status_is_exempt(self):
        status, body, _ = self._request("GET", "/tuner/auth/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertFalse(data["authenticated"])
        self.assertTrue(data["setup_required"])

    def test_protected_path_returns_401_without_cookie(self):
        status, body, _ = self._request("GET", "/tuner/status")
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "unauthenticated")

    def test_unrecognized_host_is_rejected(self):
        status, body, _ = self._request(
            "GET", "/tuner/auth/status", headers={"Host": "attacker.example"}
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid host", body)

    def test_setup_first_time_succeeds(self):
        status, body, set_cookie = self._request(
            "POST", "/tuner/setup", {"password": "abcd1234wxyz"}
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIsNotNone(set_cookie)
        self.assertIn("tuner_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIsNotNone(state.AUTH["password_hash"])

    def test_setup_when_already_configured_rejected(self):
        state.AUTH["password_hash"] = "some_hash"
        status, body, _ = self._request("POST", "/tuner/setup", {"password": "abcd1234wxyz"})
        self.assertEqual(status, 400)
        self.assertIn("already configured", body)

    def test_setup_short_password_rejected(self):
        status, body, _ = self._request("POST", "/tuner/setup", {"password": "abc"})
        self.assertEqual(status, 400)
        self.assertIn("at least 12", body)

    def test_setup_rejects_foreign_origin(self):
        status, body, _ = self._request(
            "POST",
            "/tuner/setup",
            {"password": "abcd1234wxyz"},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("forbidden origin", body)
        self.assertIsNone(state.AUTH["password_hash"])

    def test_setup_accepts_same_origin(self):
        status, body, _ = self._request(
            "POST",
            "/tuner/setup",
            {"password": "abcd1234wxyz"},
            headers={"Origin": self.base},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_setup_rejects_non_loopback_client_via_trusted_proxy(self):
        with patch.dict(
            "os.environ",
            {TRUSTED_PROXIES_ENV: "127.0.0.1/32"},
            clear=False,
        ):
            status, body, _ = self._request(
                "POST",
                "/tuner/setup",
                {"password": "abcd1234wxyz"},
                headers={"X-Forwarded-For": "198.51.100.8"},
            )
        self.assertEqual(status, 403)
        self.assertIn("restricted to loopback", body)
        self.assertIsNone(state.AUTH["password_hash"])

    def test_setup_rejects_private_non_loopback_host(self):
        status, body, _ = self._request(
            "POST",
            "/tuner/setup",
            {"password": "abcd1234wxyz"},
            headers={"Host": f"192.168.1.20:{self.port}"},
        )
        self.assertEqual(status, 403)
        self.assertIn("client and Host required", body)
        self.assertIsNone(state.AUTH["password_hash"])

    def test_duplicate_content_length_is_rejected(self):
        status, body = self._raw_post(
            "/tuner/setup",
            [
                ("Host", self.host_header),
                ("Content-Length", "1"),
                ("Content-Length", "1"),
            ],
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid content length", body)

    def test_oversized_content_length_is_rejected_before_body_read(self):
        status, body = self._raw_post(
            "/tuner/setup",
            [("Host", self.host_header), ("Content-Length", "999999999999999999999")],
        )
        self.assertEqual(status, 413)
        self.assertIn("request body too large", body)

    def test_protected_post_authenticates_before_body_framing_or_read(self):
        status, body = self._raw_post(
            "/tuner/start",
            [("Host", self.host_header), ("Content-Length", "999999999999999999999")],
        )
        self.assertEqual(status, 401)
        self.assertIn("unauthenticated", body)

    def test_login_wrong_password_returns_401(self):
        correct_pw = "correctpw"
        state.AUTH["password_hash"] = hash_password(correct_pw)
        status, body, _ = self._request("POST", "/tuner/login", {"password": "wrongpw"})
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertIn("invalid password", data["error"])

    def test_login_when_not_configured_returns_401(self):
        status, body, _ = self._request("POST", "/tuner/login", {"password": "abcd1234"})
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertEqual(data["error"], "setup_required")

    def test_login_correct_password_succeeds(self):
        correct_pw = "correctpw"
        state.AUTH["password_hash"] = hash_password(correct_pw)
        status, body, set_cookie = self._request("POST", "/tuner/login", {"password": correct_pw})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIsNotNone(set_cookie)
        self.assertIn("tuner_session=", set_cookie)
        self.assertIn(f"Max-Age={86400}", set_cookie)

    def test_logout_invalidates_session(self):
        correct_pw = "correctpw"
        state.AUTH["password_hash"] = hash_password(correct_pw)
        status, body, set_cookie = self._request("POST", "/tuner/login", {"password": correct_pw})
        self.assertEqual(status, 200)
        # Extract just the cookie value to send back
        cookie = set_cookie.split(";")[0]

        # Should be able to access protected route with session
        status, body, _ = self._request("GET", "/tuner/status", cookie=cookie)
        self.assertEqual(status, 200)

        # Logout
        status, body, _ = self._request("POST", "/tuner/logout", cookie=cookie)
        self.assertEqual(status, 200)

        # Session is now invalid
        status, body, _ = self._request("GET", "/tuner/status", cookie=cookie)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
