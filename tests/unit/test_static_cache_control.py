from unittest.mock import MagicMock, mock_open, patch

from tuner_app.http_server.handlers import static_routes


def _headers(handler):
    return {call.args[0]: call.args[1] for call in handler.send_header.call_args_list}


def test_dashboard_is_available_as_a_packaged_resource():
    dashboard = static_routes.STATIC_ROOT.joinpath("dashboard.html")

    assert dashboard.is_file()
    assert b"<!DOCTYPE html>" in dashboard.read_bytes()


def test_send_file_emits_no_store_for_js():
    handler = MagicMock()
    handler.wfile = MagicMock()

    with patch("builtins.open", mock_open(read_data=b"window.foo=1;")):
        static_routes._send_file(
            handler, "/fake/path/main.js", "application/javascript; charset=utf-8"
        )

    cache_control_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args and call.args[0] == "Cache-Control"
    ]
    assert len(cache_control_calls) == 1
    assert "no-store" in cache_control_calls[0].args[1].lower()


def test_send_file_emits_no_store_for_css():
    handler = MagicMock()
    handler.wfile = MagicMock()

    with patch("builtins.open", mock_open(read_data=b"body { color: red; }")):
        static_routes._send_file(handler, "/fake/path/style.css", "text/css; charset=utf-8")

    cache_control_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args and call.args[0] == "Cache-Control"
    ]
    assert len(cache_control_calls) == 1
    assert "no-store" in cache_control_calls[0].args[1].lower()


def test_dashboard_emits_content_security_headers():
    handler = MagicMock()
    handler.wfile = MagicMock()

    with patch("builtins.open", mock_open(read_data=b"<!doctype html>")):
        static_routes._send_file(handler, "/fake/dashboard.html", "text/html; charset=utf-8")

    headers = _headers(handler)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "object-src 'none'" in headers["Content-Security-Policy"]
    assert "camera=()" in headers["Permissions-Policy"]


def test_script_emits_nosniff_without_html_csp():
    handler = MagicMock()
    handler.wfile = MagicMock()

    with patch("builtins.open", mock_open(read_data=b"window.foo=1;")):
        static_routes._send_file(handler, "/fake/main.js", "application/javascript; charset=utf-8")

    headers = _headers(handler)
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" not in headers
