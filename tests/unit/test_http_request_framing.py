"""Request framing must be bounded and gated before any POST body is read."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest

from tuner_app.http_server import handler as handler_module
from tuner_app.http_server.handler import (
    MAX_REQUEST_BODY_BYTES,
    TunerHandler,
    _validated_content_length,
)


class _Headers(dict):
    def __init__(self, pairs=()):
        super().__init__()
        self.pairs = list(pairs)
        for key, value in self.pairs:
            self.setdefault(key, value)

    def get_all(self, name):
        values = [value for key, value in self.pairs if key.casefold() == name.casefold()]
        return values or None


class _Reader:
    def __init__(self, value=b"", events=None):
        self.value = value
        self.events = events if events is not None else []

    def read(self, length):
        self.events.append(("read", length))
        return self.value[:length]


def _handler(headers=()):
    instance = object.__new__(TunerHandler)
    instance.headers = _Headers(headers)
    instance.client_address = ("127.0.0.1", 12345)
    instance.path = "/tuner/start"
    instance.wfile = BytesIO()
    instance.rfile = _Reader()
    instance.status = None
    instance.sent_headers = []
    instance.close_connection = False
    instance.send_response = lambda status: setattr(instance, "status", status)
    instance.send_header = lambda key, value: instance.sent_headers.append((key, value))
    instance.end_headers = lambda: None
    return instance


@pytest.mark.parametrize("value", ["", "-1", "+1", "1.5", "1, 1", "abc", "\N{SUPERSCRIPT ONE}"])
def test_malformed_content_lengths_are_rejected(value):
    instance = _handler([("Content-Length", value)])
    assert _validated_content_length(instance) is None
    assert instance.status == 400
    assert instance.close_connection is True


def test_duplicate_content_lengths_are_rejected_even_when_equal():
    instance = _handler([("Content-Length", "5"), ("Content-Length", "5")])
    assert _validated_content_length(instance) is None
    assert instance.status == 400


def test_oversized_content_length_is_rejected_without_integer_dos():
    instance = _handler([("Content-Length", "9" * 10000)])
    assert _validated_content_length(instance) is None
    assert instance.status == 413


def test_content_length_limit_is_inclusive():
    instance = _handler([("Content-Length", str(MAX_REQUEST_BODY_BYTES))])
    assert _validated_content_length(instance) == MAX_REQUEST_BODY_BYTES


def test_transfer_encoding_is_rejected():
    instance = _handler([("Transfer-Encoding", "chunked")])
    assert _validated_content_length(instance) is None
    assert instance.status == 400


def test_non_exempt_post_authenticates_before_reading_body():
    events = []
    instance = _handler([("Host", "localhost"), ("Content-Length", "2")])
    instance.rfile = _Reader(b"{}", events)

    def authenticate(_handler, _method):
        events.append(("auth", None))
        return True

    def route(_handler, body):
        events.append(("route", body))

    with (
        patch.object(handler_module, "require_auth", authenticate),
        patch.dict(handler_module.ROUTES_POST, {"/tuner/start": route}, clear=True),
    ):
        instance.do_POST()

    assert events == [("auth", None), ("read", 2), ("route", b"{}")]


def test_unauthenticated_post_body_is_never_read():
    events = []
    instance = _handler([("Host", "localhost"), ("Content-Length", "100")])
    instance.rfile = _Reader(b"secret", events)
    with patch.object(handler_module, "require_auth", return_value=False):
        instance.do_POST()
    assert events == []


def test_oversized_post_body_is_never_read():
    events = []
    instance = _handler(
        [("Host", "localhost"), ("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1))]
    )
    instance.rfile = _Reader(b"secret", events)
    with patch.object(handler_module, "require_auth", return_value=True):
        instance.do_POST()
    assert instance.status == 413
    assert events == []


def test_malformed_json_is_a_bounded_client_error():
    instance = _handler([("Host", "localhost"), ("Content-Length", "1")])
    instance.path = "/tuner/setup"
    instance.rfile = _Reader(b"{")

    def parse_json(_handler, body):
        return json.loads(body)

    with patch.dict(handler_module.ROUTES_POST, {"/tuner/setup": parse_json}, clear=True):
        instance.do_POST()

    assert instance.status == 400
    assert b"invalid JSON request body" in instance.wfile.getvalue()
