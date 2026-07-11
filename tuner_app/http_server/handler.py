from __future__ import annotations

import dataclasses
import http.server
import json
import logging

from tuner_app.http_server.auth_helpers import (
    _header_values,
    require_auth,
    require_valid_host,
    require_valid_post_origin,
)
from tuner_app.http_server.routes import (
    ROUTES_GET,
    ROUTES_GET_PREFIX,
    ROUTES_POST,
    ROUTES_POST_PREFIX,
)
from tuner_app.privacy import sanitize

logger = logging.getLogger(__name__)

MAX_REQUEST_BODY_BYTES = 1024 * 1024


def _json_default(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    return str(o)


def _send_request_error(handler, status: int, error: str) -> None:
    """Send a terminal request-framing error and prevent connection reuse."""
    body = json.dumps({"ok": False, "error": error}).encode()
    handler.close_connection = True
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)


def _validated_content_length(handler) -> int | None:
    """Return a safe POST body length, or reject malformed/oversized framing."""
    if _header_values(handler, "Transfer-Encoding"):
        _send_request_error(handler, 400, "transfer encoding is not supported")
        return None

    values = _header_values(handler, "Content-Length")
    if not values:
        return 0
    if len(values) != 1:
        _send_request_error(handler, 400, "invalid content length")
        return None

    raw = values[0].strip()
    if not raw or not raw.isascii() or not raw.isdigit():
        _send_request_error(handler, 400, "invalid content length")
        return None

    normalized = raw.lstrip("0") or "0"
    maximum = str(MAX_REQUEST_BODY_BYTES)
    if len(normalized) > len(maximum) or (len(normalized) == len(maximum) and normalized > maximum):
        _send_request_error(handler, 413, "request body too large")
        return None
    return int(normalized)


class TunerHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request dispatcher for the Antminer Chip Tuner.

    Owns:
    - the `manager` class attribute (a TunerManager singleton, set by
      start_http_server before serve_forever runs).
    - the auth gate via require_auth().
    - dispatch into ROUTES_GET / ROUTES_POST registries via path matching.

    Route handlers are free functions in `tuner_app.http_server.handlers.*`.
    They access manager via `self.manager` and use the helpers `_json_response`,
    `send_response`, `send_header`, `end_headers`, `wfile` etc.
    """

    # Set by start_http_server() before serving begins; route handlers read via
    # self.manager (instance attribute access falls through to class attribute).
    manager = None

    # ---- HTTP method handlers ----

    # Connection-aborted / reset / broken-pipe at the top of any do_* handler
    # is benign: it just means the client closed before we finished
    # responding. Route handlers and response helpers (_json_response,
    # static_routes and auth_routes) all eventually call
    # self.wfile.write, and any of them can hit one of these errors when a
    # browser navigates away mid-poll or our response is slow (e.g. LuxOS
    # rate-gating). _json_response handles them locally; this backstop
    # catches the same class of error from every other write path so
    # socketserver doesn't print the traceback to stderr.
    _CLIENT_DISCONNECT_ERRORS = (
        ConnectionAbortedError,
        ConnectionResetError,
        BrokenPipeError,
    )

    def do_HEAD(self):
        try:
            if not require_valid_host(self):
                return
            if not require_auth(self, "GET"):
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
        except self._CLIENT_DISCONNECT_ERRORS as exc:
            logger.debug("Client disconnected during HEAD (path=%s): %s", self.path, exc)

    def do_GET(self):
        try:
            if not require_valid_host(self):
                return
            if not require_auth(self, "GET"):
                return
            path = self.path.split("?", 1)[0]
            # The legacy do_GET treated /, /index.html, and /?... as the dashboard.
            # /?... resolves to / after the query-strip above, which matches ROUTES_GET.
            handler_fn = ROUTES_GET.get(path)
            if handler_fn is not None:
                handler_fn(self, None)
                return
            for prefix, fn in ROUTES_GET_PREFIX:
                if self.path.startswith(prefix):
                    fn(self, None)
                    return
            self.send_response(404)
            self.end_headers()
        except self._CLIENT_DISCONNECT_ERRORS as exc:
            logger.debug("Client disconnected during GET (path=%s): %s", self.path, exc)

    def do_POST(self):
        try:
            if not require_valid_host(self):
                return
            if not require_valid_post_origin(self):
                return
            if not require_auth(self, "POST"):
                return
            content_length = _validated_content_length(self)
            if content_length is None:
                return
            body = self.rfile.read(content_length) if content_length else b""
            if len(body) != content_length:
                _send_request_error(self, 400, "incomplete request body")
                return
            path = self.path.split("?", 1)[0]
            handler_fn = ROUTES_POST.get(path)
            if handler_fn is not None:
                handler_fn(self, body)
                return
            for prefix, fn in ROUTES_POST_PREFIX:
                if self.path.startswith(prefix):
                    fn(self, body)
                    return
            self.send_response(404)
            self.end_headers()
        except (json.JSONDecodeError, UnicodeDecodeError):
            _send_request_error(self, 400, "invalid JSON request body")
        except self._CLIENT_DISCONNECT_ERRORS as exc:
            logger.debug("Client disconnected during POST (path=%s): %s", self.path, exc)

    # ---- Response helpers used by route handlers ----

    def _json_response(self, data, status=200):
        """Serialize `data` to JSON and send as the HTTP response.

        Wraps the response-emission block so that ``ConnectionAbortedError``,
        ``ConnectionResetError``, and ``BrokenPipeError`` (raised when the
        browser closes a stale connection mid-write — e.g. during slow
        ``/overview`` or ``/live`` polls when a LuxOS miner is rate-gating
        TCP commands) do not propagate up into ``socketserver`` and print
        full tracebacks to stderr. Logged at DEBUG so the signal stays
        discoverable for slow-endpoint investigations without spamming.
        Other exceptions in route handlers still propagate.
        """
        body = json.dumps(sanitize(data), default=_json_default).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as exc:
            logger.debug(
                "Client disconnected before response complete (path=%s): %s",
                getattr(self, "path", "?"),
                exc,
            )

    def log_message(self, format, *args):
        """Silence default request logging."""
        pass
