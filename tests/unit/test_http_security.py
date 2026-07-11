"""Focused HTTP trust-boundary regressions."""

from __future__ import annotations

from importlib.resources import files
from io import BytesIO
from unittest.mock import patch

from tuner_app.http_server.auth_helpers import (
    ALLOWED_HOSTS_ENV,
    SECURE_COOKIES_ENV,
    TRUSTED_PROXIES_ENV,
    get_client_ip,
    is_allowed_host,
    is_loopback_host,
    require_valid_host,
    require_valid_post_origin,
    set_session_cookie,
)
from tuner_app.http_server.handlers import auth_routes


class _Handler:
    def __init__(self, peer="198.51.100.20", headers=None):
        self.client_address = (peer, 12345)
        self.headers = headers or {}
        self.wfile = BytesIO()
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass


class _DuplicateHeaders(dict):
    def __init__(self, pairs):
        super().__init__()
        self.pairs = pairs
        for key, value in pairs:
            self.setdefault(key, value)

    def get_all(self, name):
        values = [value for key, value in self.pairs if key.casefold() == name.casefold()]
        return values or None


def test_x_forwarded_for_is_ignored_without_explicit_trust():
    handler = _Handler(headers={"X-Forwarded-For": "127.0.0.1"})
    with patch.dict("os.environ", {}, clear=True):
        assert get_client_ip(handler) == "198.51.100.20"


def test_x_forwarded_for_is_used_only_for_allowlisted_proxy():
    handler = _Handler(
        peer="192.0.2.10",
        headers={"X-Forwarded-For": "198.51.100.8"},
    )
    with patch.dict("os.environ", {TRUSTED_PROXIES_ENV: "192.0.2.10/32"}, clear=True):
        assert get_client_ip(handler) == "198.51.100.8"


def test_duplicate_x_forwarded_for_is_ignored_even_for_trusted_proxy():
    handler = _Handler(
        peer="192.0.2.10",
        headers=_DuplicateHeaders(
            [
                ("X-Forwarded-For", "127.0.0.1"),
                ("X-Forwarded-For", "198.51.100.8"),
            ]
        ),
    )
    with patch.dict("os.environ", {TRUSTED_PROXIES_ENV: "192.0.2.10/32"}, clear=True):
        assert get_client_ip(handler) == "192.0.2.10"


def test_untrusted_leftmost_forwarded_value_cannot_spoof_client():
    handler = _Handler(
        peer="192.0.2.10",
        headers={"X-Forwarded-For": "127.0.0.1, 198.51.100.8"},
    )
    with patch.dict("os.environ", {TRUSTED_PROXIES_ENV: "192.0.2.10/32"}, clear=True):
        assert get_client_ip(handler) == "198.51.100.8"


def test_default_host_policy_allows_localhost_and_private_ip_literals():
    with patch.dict("os.environ", {}, clear=True):
        assert is_allowed_host(_Handler(headers={"Host": "localhost:8099"}))
        assert is_allowed_host(_Handler(headers={"Host": "192.168.1.20:8099"}))
        assert is_allowed_host(_Handler(headers={"Host": "[::1]:8099"}))


def test_unknown_dns_and_public_ip_hosts_are_rejected():
    with patch.dict("os.environ", {}, clear=True):
        assert not is_allowed_host(_Handler(headers={"Host": "attacker.example"}))
        assert not is_allowed_host(_Handler(headers={"Host": "8.8.8.8"}))


def test_exact_dns_host_can_be_explicitly_allowlisted():
    handler = _Handler(headers={"Host": "TUNER.HOME.ARPA:8099"})
    with patch.dict("os.environ", {ALLOWED_HOSTS_ENV: "tuner.home.arpa"}, clear=True):
        assert is_allowed_host(handler)
        assert not is_loopback_host(handler)


def test_duplicate_host_headers_are_rejected():
    handler = _Handler(
        headers=_DuplicateHeaders([("Host", "localhost"), ("Host", "attacker.example")])
    )
    assert require_valid_host(handler) is False
    assert handler.status == 400
    assert b"invalid host" in handler.wfile.getvalue()


def test_loopback_host_does_not_trust_forwarded_host():
    handler = _Handler(headers={"Host": "192.168.1.20:8099", "X-Forwarded-Host": "localhost:8099"})
    assert not is_loopback_host(handler)


def test_setup_requires_loopback_host_even_for_loopback_client():
    handler = _Handler(peer="127.0.0.1", headers={"Host": "192.168.1.20:8099"})
    responses = []
    handler._json_response = lambda payload, status=200: responses.append((status, payload))
    auth_routes.setup(handler, b'{"password":"abcd1234wxyz"}')
    assert responses == [
        (
            403,
            {
                "ok": False,
                "error": "initial setup is restricted to loopback (client and Host required)",
            },
        )
    ]


def test_setup_ui_enforces_twelve_character_minimum():
    script = (files("tuner_app") / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert "pw.length < 12" in script
    assert "Password must be at least 12 characters." in script
    assert "Password must be at least 4 characters." not in script


def test_foreign_post_origin_is_rejected():
    handler = _Handler(headers={"Host": "localhost:8080", "Origin": "https://attacker.example"})
    assert require_valid_post_origin(handler) is False
    assert handler.status == 403
    assert b"forbidden origin" in handler.wfile.getvalue()


def test_same_post_origin_is_accepted():
    handler = _Handler(headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"})
    assert require_valid_post_origin(handler) is True
    assert handler.status is None


def test_secure_cookie_attribute_is_explicitly_opt_in():
    handler = _Handler()
    with patch.dict("os.environ", {}, clear=True):
        set_session_cookie(handler, "test-token")
    assert "Secure" not in dict(handler.sent_headers)["Set-Cookie"]

    handler.sent_headers.clear()
    with patch.dict("os.environ", {SECURE_COOKIES_ENV: "1"}, clear=True):
        set_session_cookie(handler, "test-token")
    assert "; Secure" in dict(handler.sent_headers)["Set-Cookie"]
