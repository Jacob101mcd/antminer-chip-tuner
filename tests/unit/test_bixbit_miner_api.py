"""Tests for BixbitMinerAPI TCP/JSON socket client."""

import json
import socket
from unittest.mock import MagicMock, patch

import pytest

from tuner_app.miner.bixbit import BixbitMinerAPI
from tuner_app.miner.exceptions import (
    MinerCommandError,
    MinerOfflineError,
    UnsafeVoltageBoundsError,
)


@pytest.fixture
def miner():
    return BixbitMinerAPI("1.2.3.4")


def _make_sock_with_response(response_dict):
    mock_sock = MagicMock()
    data = json.dumps(response_dict).encode()
    mock_sock.recv.side_effect = [data, b""]
    mock_sock.__enter__ = lambda s: mock_sock
    mock_sock.__exit__ = MagicMock(return_value=False)
    return mock_sock


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_summary_happy_path(mock_connect, miner):
    mock_sock = _make_sock_with_response({"STATUS": "S", "MHS av": 200.0})
    mock_connect.return_value = mock_sock
    result = miner._summary_raw()
    assert result == {"STATUS": "S", "MHS av": 200.0}


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_set_voltage_refuses_unverified_fallback_bounds(mock_connect, miner):
    mock_sock = _make_sock_with_response({"STATUS": "S"})
    mock_connect.return_value = mock_sock
    with pytest.raises(UnsafeVoltageBoundsError, match="unverified"):
        miner.set_voltage(14000)
    sent = mock_sock.sendall.call_args[0][0]
    parsed = json.loads(sent.decode().rstrip("\n"))
    assert parsed["cmd"] == "get_board_slots_state"
    assert "voltage_target" not in parsed


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_set_clock_all_happy_path(mock_connect, miner):
    mock_sock = _make_sock_with_response({"STATUS": "S"})
    mock_connect.return_value = mock_sock
    result = miner.set_clock_all(490)
    sent = mock_sock.sendall.call_args[0][0]
    parsed = json.loads(sent.decode().rstrip("\n"))
    assert parsed["cmd"] == "set_overclock_info"
    assert parsed["freq_target"] == 490
    assert result is True


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_set_power_limit_happy_path(mock_connect, miner):
    mock_sock = _make_sock_with_response({"STATUS": "S"})
    mock_connect.return_value = mock_sock
    result = miner.set_power_limit(3500)
    sent = mock_sock.sendall.call_args[0][0]
    parsed = json.loads(sent.decode().rstrip("\n"))
    assert parsed["cmd"] == "set_user_power_limit"
    assert parsed["powerLimit"] == 3500
    assert parsed["powerMode"] == "Normal"
    assert parsed["softRestart"] is True
    assert result is True


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_status_e_raises_command_error(mock_connect, miner):
    mock_sock = _make_sock_with_response(
        {"STATUS": "E", "Code": 132, "Msg": "API command ERROR", "Description": ""}
    )
    mock_connect.return_value = mock_sock
    with pytest.raises(MinerCommandError):
        miner.summary()


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_socket_timeout_raises_offline(mock_connect, miner):
    mock_connect.side_effect = socket.timeout
    with pytest.raises(MinerOfflineError):
        miner.summary()


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_connection_refused_raises_offline(mock_connect, miner):
    mock_connect.side_effect = ConnectionRefusedError
    with pytest.raises(MinerOfflineError):
        miner.summary()


def test_temps_chip_raises_not_implemented(miner):
    assert miner.temps_chip() == []


def test_set_clock_chip_raises_not_implemented(miner):
    with pytest.raises(NotImplementedError):
        miner.set_clock_chip(0, [])


def test_set_clock_board_raises_not_implemented(miner):
    with pytest.raises(NotImplementedError):
        miner.set_clock_board([])


def test_set_coin_raises_not_implemented(miner):
    with pytest.raises(NotImplementedError):
        miner.set_coin("BTC", [])


def test_firmware_type_returns_bixbit(miner):
    assert miner.firmware_type() == "bixbit"


def test_set_perpetualtune_is_noop(miner):
    with patch("tuner_app.miner.bixbit.socket.create_connection") as mock_connect:
        result = miner.set_perpetualtune(True)
        assert result is True
        assert not mock_connect.called


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_summary_hashrate_ths_converts_units(mock_connect, miner):
    mock_sock = _make_sock_with_response({"STATUS": "S", "HS RT": 200_000_000})
    mock_connect.return_value = mock_sock
    result = miner.summary()
    assert result.hashrate_ths == 200.0

    mock_sock = _make_sock_with_response({"STATUS": "S", "HS RT": 0})
    mock_connect.return_value = mock_sock
    result = miner.summary()
    assert result.hashrate_ths == 0.0


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_authenticate_connectivity_check_success(mock_connect, miner):
    mock_sock = _make_sock_with_response({"STATUS": "S"})
    mock_connect.return_value = mock_sock
    assert miner.authenticate() is True


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_authenticate_connectivity_check_failure(mock_connect, miner):
    mock_connect.side_effect = socket.timeout
    assert miner.authenticate() is False


@patch("tuner_app.miner.bixbit.socket.create_connection")
def test_socket_closes_on_exception(mock_connect, miner):
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [b'{"STATUS":"E","Msg":"err","Description":""}', b""]
    mock_sock.__enter__ = lambda s: mock_sock
    mock_sock.__exit__ = MagicMock(return_value=False)
    mock_connect.return_value = mock_sock
    with pytest.raises(MinerCommandError):
        miner.summary()
    mock_sock.__exit__.assert_called_once()
