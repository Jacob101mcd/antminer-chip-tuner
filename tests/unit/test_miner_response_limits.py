"""Hard-cap regression tests for untrusted miner network responses."""

from __future__ import annotations

from unittest import mock
from unittest.mock import MagicMock

import pytest

from tuner_app.miner.bixbit import BixbitMinerAPI
from tuner_app.miner.braiins import BraiinsMinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MRRError
from tuner_app.miner.luxos import _LuxosTransport
from tuner_app.miner.whatsminer import WhatsminerMinerAPI
from tuner_app.mrr.client import MRRClient
from tuner_app.net.http_client import miner_http_request
from tuner_app.profit.minerstat import MinerstatError, fetch_minerstat_coins
from tuner_app.scanner import discover

_TEST_CAP = 16
_FIRST_INVALID_CHUNK = b"{" + (b"x" * (_TEST_CAP - 1))
_SECRET_OVERFLOW = b"private-response-body"


def _socket_with_oversized_response() -> MagicMock:
    sock = MagicMock()
    sock.recv.side_effect = [_FIRST_INVALID_CHUNK, _SECRET_OVERFLOW, b""]
    sock.__enter__.return_value = sock
    sock.__exit__.return_value = False
    return sock


def _http_connection_with_body(body: bytes) -> tuple[MagicMock, MagicMock]:
    response = MagicMock()
    response.status = 200
    response.getheaders.return_value = []
    response.read.return_value = body
    connection = MagicMock()
    connection.getresponse.return_value = response
    return connection, response


@mock.patch("tuner_app.net.http_client.http.client.HTTPConnection")
def test_miner_http_request_rejects_oversized_body_without_leaking_it(mock_http) -> None:
    connection, response = _http_connection_with_body(_SECRET_OVERFLOW)
    mock_http.return_value = connection

    with (
        mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP),
        pytest.raises(MinerCommandError) as exc_info,
    ):
        miner_http_request("192.0.2.10", 4028, "/summary", source_ip="")

    assert "exceeded 16 byte cap" in str(exc_info.value)
    assert "private-response-body" not in str(exc_info.value)
    response.read.assert_called_once_with(_TEST_CAP + 1)
    connection.close.assert_called_once()


@mock.patch("tuner_app.net.http_client.http.client.HTTPConnection")
def test_miner_http_request_accepts_body_exactly_at_cap(mock_http) -> None:
    body = b"x" * _TEST_CAP
    connection, _response = _http_connection_with_body(body)
    mock_http.return_value = connection

    with mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP):
        status, headers, result = miner_http_request("192.0.2.10", 4028, "/summary", source_ip="")

    assert (status, headers, result) == (200, [], body)


@pytest.mark.parametrize("transport", ["plaintext", "raw"])
@mock.patch("tuner_app.miner.whatsminer.socket.create_connection")
def test_whatsminer_rejects_oversized_response_without_leaking_it(
    mock_connect, transport: str
) -> None:
    mock_connect.return_value = _socket_with_oversized_response()
    miner = WhatsminerMinerAPI("192.0.2.10")

    with (
        mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP),
        pytest.raises(MinerCommandError) as exc_info,
    ):
        if transport == "plaintext":
            miner._send_plaintext("summary")
        else:
            miner._send_raw({"cmd": "summary"})

    assert "exceeded 16 byte cap" in str(exc_info.value)
    assert "private-response-body" not in str(exc_info.value)


@mock.patch("tuner_app.miner.bixbit.socket.create_connection")
def test_bixbit_rejects_oversized_response_without_leaking_it(mock_connect) -> None:
    mock_connect.return_value = _socket_with_oversized_response()

    with (
        mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP),
        pytest.raises(MinerCommandError) as exc_info,
    ):
        BixbitMinerAPI("192.0.2.10")._send_cmd("summary")

    assert "exceeded 16 byte cap" in str(exc_info.value)
    assert "private-response-body" not in str(exc_info.value)


@pytest.mark.parametrize("vendor", ["whatsminer", "bixbit", "luxos"])
def test_incomplete_tcp_response_error_reports_only_byte_count(vendor: str) -> None:
    sock = MagicMock()
    sock.recv.side_effect = [_SECRET_OVERFLOW, b""]
    sock.__enter__.return_value = sock
    sock.__exit__.return_value = False

    patch_target = f"tuner_app.miner.{vendor}.socket.create_connection"
    with mock.patch(patch_target, return_value=sock), pytest.raises(MinerCommandError) as exc_info:
        if vendor == "whatsminer":
            WhatsminerMinerAPI("192.0.2.10")._send_plaintext("summary")
        elif vendor == "bixbit":
            BixbitMinerAPI("192.0.2.10")._send_cmd("summary")
        else:
            _LuxosTransport("192.0.2.10", min_conn_interval_sec=0, offline_backoff_sec=0)._send_raw(
                {"command": "summary"}
            )

    assert "incomplete response" in str(exc_info.value)
    assert f"{len(_SECRET_OVERFLOW)} bytes" in str(exc_info.value)
    assert "private-response-body" not in str(exc_info.value)


@mock.patch("http.client.HTTPConnection")
@mock.patch("tuner_app.net.source_ip.resolve_source_ip", return_value="")
def test_braiins_authenticated_http_rejects_oversized_body(_mock_source_ip, mock_http) -> None:
    connection, response = _http_connection_with_body(_SECRET_OVERFLOW)
    mock_http.return_value = connection
    miner = BraiinsMinerAPI("192.0.2.10", port=80)

    with (
        mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP),
        pytest.raises(MinerCommandError) as exc_info,
    ):
        miner._raw_request_with_token("/api/v1/status", "GET", None)

    assert "exceeded 16 byte cap" in str(exc_info.value)
    assert "private-response-body" not in str(exc_info.value)
    response.read.assert_called_once_with(_TEST_CAP + 1)
    connection.close.assert_called_once()


@mock.patch("tuner_app.mrr.client.http.client.HTTPSConnection")
def test_mrr_rejects_oversized_response_without_disclosing_body(mock_https) -> None:
    connection, response = _http_connection_with_body(_SECRET_OVERFLOW)
    mock_https.return_value = connection

    with (
        mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP),
        pytest.raises(MRRError) as exc_info,
    ):
        MRRClient("example-key", "example-secret").whoami()

    assert "response limit" in str(exc_info.value)
    assert "private-response-body" not in str(exc_info.value)
    response.read.assert_called_once_with(_TEST_CAP + 1)
    connection.close.assert_called_once()


@mock.patch("tuner_app.profit.minerstat.http.client.HTTPSConnection")
def test_minerstat_rejects_oversized_response_without_disclosing_body(mock_https) -> None:
    connection, response = _http_connection_with_body(_SECRET_OVERFLOW)
    mock_https.return_value = connection

    with (
        mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP),
        pytest.raises(MinerstatError) as exc_info,
    ):
        fetch_minerstat_coins(["BTC"])

    assert "response limit" in str(exc_info.value)
    assert "private-response-body" not in str(exc_info.value)
    response.read.assert_called_once_with(_TEST_CAP + 1)
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    ("probe", "args", "expected"),
    [
        (discover._probe_whatsminer_tcp, ("192.0.2.10", 4028, 0.1), None),
        (discover._probe_bixbit_tcp, ("192.0.2.10", 4028, 0.1), None),
        (discover._probe_luxos_tcp, ("192.0.2.10", 4028, 0.1), None),
        (
            discover._validate_whatsminer_password,
            ("192.0.2.10", 4028, "example-password", "salt", 0.1),
            False,
        ),
        (
            discover._validate_bixbit_password,
            ("192.0.2.10", 4028, "example-password", 0.1),
            False,
        ),
        (
            discover._validate_luxos_password,
            ("192.0.2.10", 4028, "example-password", 0.1),
            False,
        ),
    ],
)
@mock.patch("tuner_app.scanner.discover.socket.create_connection")
def test_scanner_tcp_probes_fail_closed_on_oversized_response(
    mock_connect, probe, args, expected
) -> None:
    mock_connect.return_value = _socket_with_oversized_response()

    with mock.patch("tuner_app.net.response_limits.MINER_RESPONSE_HARD_CAP_BYTES", _TEST_CAP):
        assert probe(*args) is expected


@mock.patch("tuner_app.scanner.discover.time.sleep")
@mock.patch("tuner_app.scanner.discover.socket.create_connection")
def test_luxos_config_probe_checks_cap_before_appending(mock_connect, _mock_sleep) -> None:
    sock = MagicMock()
    sock.recv.side_effect = [b"x" * (_TEST_CAP + 1), b""]
    sock.__enter__.return_value = sock
    sock.__exit__.return_value = False
    mock_connect.return_value = sock

    with mock.patch("tuner_app.scanner.discover._MAX_LUXOS_CONFIG_BYTES", _TEST_CAP):
        assert discover._fetch_luxos_config("192.0.2.10", 4028, 0.1) is None
