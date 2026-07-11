"""Tests that the HTTP handler swallows client-disconnect errors silently.

When a browser closes a stale connection mid-write (common during slow
``/overview`` or ``/live`` polls when a LuxOS miner is rate-gating TCP
commands), ``self.wfile.write`` raises ``ConnectionAbortedError``,
``ConnectionResetError``, or ``BrokenPipeError``. Pre-fix these escaped
into ``socketserver.process_request_thread`` and printed full tracebacks
to stderr — making the operator think the server crashed when it had
not. ``_json_response`` and the do_* backstops should swallow these
specific errors and DEBUG-log them instead.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from tuner_app.http_server.handler import TunerHandler


class _FakeWfile:
    def __init__(self, raise_exc=None):
        self._raise_exc = raise_exc
        self.written = []

    def write(self, data):
        if self._raise_exc is not None:
            raise self._raise_exc
        self.written.append(data)


class _BareHandler:
    """Minimum mock of TunerHandler bound to ``_json_response`` for unit-testing
    write-path exception handling without spinning up the full request handler."""

    _json_response = TunerHandler._json_response
    _CLIENT_DISCONNECT_ERRORS = TunerHandler._CLIENT_DISCONNECT_ERRORS

    def __init__(self, write_exc=None):
        self.path = "/tuner/overview"
        self.wfile = _FakeWfile(write_exc)
        self.send_response = MagicMock()
        self.send_header = MagicMock()
        self.end_headers = MagicMock()


class TestJsonResponseSwallowsClientDisconnect(unittest.TestCase):
    def test_connection_aborted_silently_swallowed(self):
        h = _BareHandler(write_exc=ConnectionAbortedError("WinError 10053"))
        # Must not raise — pre-fix this propagated to socketserver.
        h._json_response({"ok": True})

    def test_connection_reset_silently_swallowed(self):
        h = _BareHandler(write_exc=ConnectionResetError("reset"))
        h._json_response({"ok": True})

    def test_broken_pipe_silently_swallowed(self):
        h = _BareHandler(write_exc=BrokenPipeError("pipe"))
        h._json_response({"ok": True})

    def test_unrelated_exception_propagates(self):
        # A bug in JSON serialization or the route handler should NOT be
        # swallowed by the disconnect catch — only the three specific
        # client-disconnect classes.
        h = _BareHandler(write_exc=RuntimeError("real bug"))
        with self.assertRaises(RuntimeError):
            h._json_response({"ok": True})

    def test_clean_write_succeeds(self):
        h = _BareHandler()
        h._json_response({"ok": True, "value": 42})
        self.assertEqual(len(h.wfile.written), 1)
        body = h.wfile.written[0]
        self.assertIn(b'"ok": true', body)
        self.assertIn(b'"value": 42', body)


if __name__ == "__main__":
    unittest.main()
