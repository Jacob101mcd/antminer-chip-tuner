from __future__ import annotations

import json
import os
from unittest import mock
from unittest.mock import MagicMock

import pytest

from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.whatsminer import (
    WhatsminerMinerAPI,
    _compute_aeskey,
    _compute_sign,
    _decrypt,
    _encrypt,
    _md5crypt_hash,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "fixtures", "whatsminer_auth_vectors.json"
)
with open(FIXTURE_PATH) as f:
    AUTH_FIXTURE = json.load(f)


# Helper copied verbatim from tests/unit/test_bixbit_miner_api.py so the same
# context-manager-shaped MagicMock pattern is used here.
def _make_sock_with_response(response_bytes_or_dict):
    if isinstance(response_bytes_or_dict, (bytes, bytearray)):
        data = bytes(response_bytes_or_dict)
    else:
        data = json.dumps(response_bytes_or_dict).encode()
    mock_sock = MagicMock()
    mock_sock.recv.side_effect = [data, b""]
    mock_sock.__enter__ = lambda s: mock_sock
    mock_sock.__exit__ = MagicMock(return_value=False)
    return mock_sock


def test_md5crypt_hash_matches_fixture():
    assert (
        _md5crypt_hash(AUTH_FIXTURE["password"], AUTH_FIXTURE["salt"])
        == AUTH_FIXTURE["expected_md5crypt_hash"]
    )


def test_compute_aeskey_matches_fixture():
    assert (
        _compute_aeskey(AUTH_FIXTURE["password"], AUTH_FIXTURE["salt"]).hex()
        == AUTH_FIXTURE["expected_aeskey_hex"]
    )


def test_compute_sign_matches_fixture():
    assert (
        _compute_sign(
            AUTH_FIXTURE["password"],
            AUTH_FIXTURE["salt"],
            AUTH_FIXTURE["newsalt"],
            AUTH_FIXTURE["time"],
        )
        == AUTH_FIXTURE["expected_sign"]
    )


def test_encrypt_round_trip_matches_fixture():
    aeskey = bytes.fromhex(AUTH_FIXTURE["expected_aeskey_hex"])
    assert (
        _encrypt(AUTH_FIXTURE["round_trip_plaintext"], aeskey)
        == AUTH_FIXTURE["round_trip_ciphertext_b64"]
    )


def test_decrypt_round_trip_matches_fixture():
    aeskey = bytes.fromhex(AUTH_FIXTURE["expected_aeskey_hex"])
    assert (
        _decrypt(AUTH_FIXTURE["round_trip_ciphertext_b64"], aeskey)
        == AUTH_FIXTURE["round_trip_plaintext"]
    )


@pytest.mark.parametrize("length", [1, 15, 16, 17, 31, 32])
def test_encrypt_decrypt_round_trip(length):
    plaintext = "a" * length
    aeskey = bytes.fromhex(AUTH_FIXTURE["expected_aeskey_hex"])
    ciphertext = _encrypt(plaintext, aeskey)
    decrypted = _decrypt(ciphertext, aeskey)
    assert decrypted == plaintext


# Aux-cmd response defaults used by tests that exercise summary() but don't
# care about model/hostname enrichment. summary() now does 4 plaintext calls
# (summary + devs + get_version + get_miner_info); these stubs cover the
# last two so each test only needs to spell out the first two.
_AUX_VERSION_RESP = b'{"STATUS":"S","Code":131,"Msg":{}}'
_AUX_MINER_INFO_RESP = b'{"STATUS":"S","Code":131,"Msg":{}}'


def test_summary_happy_path():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        sock1 = _make_sock_with_response(b'{"STATUS":"S","SUMMARY":[{}],"DEVS":[]}')
        sock2 = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{}]}')
        sock3 = _make_sock_with_response(_AUX_VERSION_RESP)
        sock4 = _make_sock_with_response(_AUX_MINER_INFO_RESP)
        mock_socket.side_effect = [sock1, sock2, sock3, sock4]
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.summary()
        assert len(result.boards) == 1


def test_summary_field_mapping():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        sock1 = _make_sock_with_response(
            b'{"STATUS":"S","MHS av":1000000,"Miner Type":"Test Model"}'
        )
        sock2 = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{}]}')
        sock3 = _make_sock_with_response(_AUX_VERSION_RESP)
        sock4 = _make_sock_with_response(_AUX_MINER_INFO_RESP)
        mock_socket.side_effect = [sock1, sock2, sock3, sock4]
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.summary()
        assert result.hashrate_ths == 1.0
        assert result.model == "Test Model"


def test_summary_devs_failure_returns_empty_boards():
    """When the per-board `devs` follow-up fails (e.g. firmware rejects with
    Code 14 'invalid cmd' or 'API btminer unknow err'), summary() still
    returns a populated MinerSummary with an empty boards list — the
    primary summary cmd response is preserved.
    """
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        sock1 = _make_sock_with_response(b'{"STATUS":"S","SUMMARY":[{}],"DEVS":[]}')
        sock2 = _make_sock_with_response(b'{"STATUS":"E","Code":123,"Msg":"err"}')
        sock3 = _make_sock_with_response(_AUX_VERSION_RESP)
        sock4 = _make_sock_with_response(_AUX_MINER_INFO_RESP)
        mock_socket.side_effect = [sock1, sock2, sock3, sock4]
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.summary()
        assert len(result.boards) == 0


def test_summary_uses_devs_not_edevs_cmd():
    """Regression: H616-platform M-series firmware (M66S++_VM30 fw 2025.12)
    rejects `edevs` with `API btminer unknow err`, stalling Phase 0
    discovery indefinitely. The cgminer-standard `devs` cmd works on every
    Whatsminer btminer build observed live and returns the same per-board
    DEVS shape. summary() must send `devs` (not `edevs`) on the second
    socket.
    """
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        sock1 = _make_sock_with_response(b'{"STATUS":"S","SUMMARY":[{}],"DEVS":[]}')
        sock2 = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{}]}')
        sock3 = _make_sock_with_response(_AUX_VERSION_RESP)
        sock4 = _make_sock_with_response(_AUX_MINER_INFO_RESP)
        mock_socket.side_effect = [sock1, sock2, sock3, sock4]
        api = WhatsminerMinerAPI("192.168.1.1")
        api.summary()
        sent = sock2.sendall.call_args[0][0]
        payload = json.loads(sent.decode("utf-8").strip())
        assert payload == {"cmd": "devs"}


def test_hardware_topology_uses_devs_cmd():
    """Regression: `hardware_topology()` must send `devs` (not `edevs`)
    so it works on H616-platform firmwares.
    """
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        sock = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{},{},{},{}]}')
        mock_socket.return_value = sock
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.hardware_topology()
        assert result.num_boards == 4
        sent = sock.sendall.call_args[0][0]
        payload = json.loads(sent.decode("utf-8").strip())
        assert payload == {"cmd": "devs"}


def test_devs_method_sends_devs_cmd():
    """The public devs() helper used by wait_for_upfreq_complete sends `devs`."""
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        sock = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{"Upfreq Complete":1}]}')
        mock_socket.return_value = sock
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.devs()
        assert result.get("DEVS")[0].get("Upfreq Complete") == 1
        sent = sock.sendall.call_args[0][0]
        payload = json.loads(sent.decode("utf-8").strip())
        assert payload == {"cmd": "devs"}


def test_send_plaintext_socket_timeout_raises_offline():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.side_effect = TimeoutError()
        api = WhatsminerMinerAPI("192.168.1.1")
        with pytest.raises(MinerOfflineError):
            api._send_plaintext("test")


def test_send_plaintext_connection_refused_raises_offline():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.side_effect = ConnectionRefusedError()
        api = WhatsminerMinerAPI("192.168.1.1")
        with pytest.raises(MinerOfflineError):
            api._send_plaintext("test")


def test_send_plaintext_status_e_raises_command_error():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.return_value = _make_sock_with_response(
            b'{"STATUS":"E","Code":123,"Msg":"err"}'
        )
        api = WhatsminerMinerAPI("192.168.1.1")
        with pytest.raises(MinerCommandError):
            api._send_plaintext("test")


def test_supports_per_chip_tuning_false():
    api = WhatsminerMinerAPI("192.168.1.1")
    assert api.supports_per_chip_tuning() is False


def test_has_external_power_limit_true():
    api = WhatsminerMinerAPI("192.168.1.1")
    assert api.has_external_power_limit() is True


def test_has_capabilities_endpoint_false():
    api = WhatsminerMinerAPI("192.168.1.1")
    assert api.has_capabilities_endpoint() is False


def test_has_internal_perpetual_tune_true():
    api = WhatsminerMinerAPI("192.168.1.1")
    assert api.has_internal_perpetual_tune() is True


def test_firmware_type_returns_whatsminer():
    api = WhatsminerMinerAPI("192.168.1.1")
    assert api.firmware_type() == "whatsminer"


def test_tuning_strategy_returns_power_limit_freq_search():
    api = WhatsminerMinerAPI("192.168.1.1")
    assert api.tuning_strategy() == "power_limit_freq_search"


def test_set_voltage_raises_not_implemented():
    api = WhatsminerMinerAPI("192.168.1.1")
    with pytest.raises(NotImplementedError):
        api.set_voltage(12.0)


def test_set_clock_all_raises_not_implemented():
    api = WhatsminerMinerAPI("192.168.1.1")
    with pytest.raises(NotImplementedError):
        api.set_clock_all(100)


def test_set_clock_board_raises_not_implemented():
    api = WhatsminerMinerAPI("192.168.1.1")
    with pytest.raises(NotImplementedError):
        api.set_clock_board([])


def test_set_clock_chip_raises_not_implemented():
    api = WhatsminerMinerAPI("192.168.1.1")
    with pytest.raises(NotImplementedError):
        api.set_clock_chip(0, [])


def test_set_coin_raises_not_implemented():
    api = WhatsminerMinerAPI("192.168.1.1")
    with pytest.raises(NotImplementedError):
        api.set_coin("BTC", [])


def test_set_perpetualtune_is_noop():
    api = WhatsminerMinerAPI("192.168.1.1")
    assert api.set_perpetualtune(True) is True


def test_set_power_limit_happy_path():
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.return_value = {"STATUS": "S"}
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.set_power_limit(3500)
        mock_send.assert_called_once_with("adjust_power_limit", power_limit="3500")
        assert result is True


def test_set_power_limit_code_45_permission_denied_no_retry():
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.side_effect = MinerCommandError("Code 45")
        api = WhatsminerMinerAPI("192.168.1.1")
        with pytest.raises(MinerCommandError):
            api.set_power_limit(3500)


def test_set_power_limit_no_outer_retry_on_single_success():
    """Hazard 2: outer set_power_limit must call _send_encrypted exactly once on success.

    The inner _send_encrypted handles its own Code 135/136 retry logic; an
    outer try/except retry layer would double the socket-call budget on every
    transient token-expired event.
    """
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.return_value = {"STATUS": "S"}
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.set_power_limit(3500)
        assert result is True
        assert mock_send.call_count == 1


def test_set_power_limit_no_outer_retry_on_persistent_code_135():
    """Hazard 2: a persistent Code 135 from the inner layer must NOT trigger an outer retry.

    The inner _send_encrypted already exhausted its single force-refresh retry
    before raising. With the dual-layer-retry bug present, this assertion
    fails (mock_send.call_count == 2 instead of 1).
    """
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.side_effect = MinerCommandError("Code 135 token expired")
        api = WhatsminerMinerAPI("192.168.1.1")
        with pytest.raises(MinerCommandError):
            api.set_power_limit(3500)
        assert mock_send.call_count == 1


def test_set_power_limit_passes_through_arbitrary_exception():
    """Hazard 2: a non-token-expired MinerCommandError must propagate without retry."""
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.side_effect = MinerCommandError("Code 999 unknown error")
        api = WhatsminerMinerAPI("192.168.1.1")
        with pytest.raises(MinerCommandError):
            api.set_power_limit(3500)
        assert mock_send.call_count == 1


def test_set_power_limit_no_force_refresh_token_at_outer_layer():
    """Hazard 2: outer set_power_limit must not invoke _get_token directly.

    Token refresh on Code 135/136 is the inner _send_encrypted's responsibility.
    The outer layer is a thin one-shot dispatcher; any direct _get_token call
    here is the dual-layer-retry bug surface.
    """
    api = WhatsminerMinerAPI("192.168.1.1")
    with (
        mock.patch.object(api, "_send_encrypted") as mock_send,
        mock.patch.object(api, "_get_token") as mock_token,
    ):
        mock_send.side_effect = MinerCommandError("Code 135 token expired")
        mock_token.return_value = (b"\x00" * 32, "stub")
        with pytest.raises(MinerCommandError):
            api.set_power_limit(3500)
        assert mock_token.call_count == 0


def test_set_power_mode_low_dispatches_to_set_low_power():
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.return_value = {"STATUS": "S"}
        api = WhatsminerMinerAPI("192.168.1.1")
        api.set_power_mode("low")
        mock_send.assert_called_once_with("set_low_power")


def test_set_power_mode_normal_dispatches_to_set_normal_power():
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.return_value = {"STATUS": "S"}
        api = WhatsminerMinerAPI("192.168.1.1")
        api.set_power_mode("normal")
        mock_send.assert_called_once_with("set_normal_power")


def test_set_power_mode_high_dispatches_to_set_high_power():
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.return_value = {"STATUS": "S"}
        api = WhatsminerMinerAPI("192.168.1.1")
        api.set_power_mode("high")
        mock_send.assert_called_once_with("set_high_power")


def test_set_power_mode_case_insensitive():
    with mock.patch.object(WhatsminerMinerAPI, "_send_encrypted") as mock_send:
        mock_send.return_value = {"STATUS": "S"}
        api = WhatsminerMinerAPI("192.168.1.1")
        api.set_power_mode("LOW")
        mock_send.assert_called_once_with("set_low_power")


def test_set_power_mode_unknown_raises_value_error():
    api = WhatsminerMinerAPI("192.168.1.1")
    with pytest.raises(ValueError):
        api.set_power_mode("medium")


def test_authenticate_happy_path():
    api = WhatsminerMinerAPI("192.168.1.1")
    with (
        mock.patch.object(api, "_send_plaintext") as mock_pt,
        mock.patch.object(api, "_get_token") as mock_tok,
    ):
        mock_pt.return_value = {"STATUS": "S"}
        mock_tok.return_value = (b"\x00" * 32, "stub")
        assert api.authenticate() is True


def test_authenticate_failure_returns_false():
    with mock.patch.object(WhatsminerMinerAPI, "_send_plaintext") as mock_plaintext:
        mock_plaintext.side_effect = MinerCommandError("test")
        api = WhatsminerMinerAPI("192.168.1.1")
        assert api.authenticate() is False


def test_hardware_topology_uses_len_devs():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.return_value = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{},{},{}]}')
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.hardware_topology()
        assert result.num_boards == 3


def test_hardware_topology_chips_per_board_zero():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.return_value = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{},{},{}]}')
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.hardware_topology()
        assert result.chips_per_board == 0


def test_hardware_topology_psu_bounds():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.return_value = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{},{},{}]}')
        api = WhatsminerMinerAPI("192.168.1.1")
        result = api.hardware_topology()
        assert result.psu_min_mv > 0
        assert result.psu_max_mv > result.psu_min_mv
        assert result.psu_bounds_verified is False
        assert result.psu_bounds_source == "not-applicable:firmware-owned-vf"


def test_hardware_topology_cached_on_second_call():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.return_value = _make_sock_with_response(b'{"STATUS":"S","DEVS":[{},{},{}]}')
        api = WhatsminerMinerAPI("192.168.1.1")
        result1 = api.hardware_topology()
        result2 = api.hardware_topology()
        assert result1 == result2
        # Second hardware_topology call hits the cache — no new socket connection.
        assert mock_socket.call_count == 1


def test_get_token_caches_within_ttl():
    token_bytes = (
        b'{"STATUS":"S","Code":133,"Msg":{"salt":"abc","newsalt":"def","time":"1234567890"}}'
    )
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_socket:
        mock_socket.side_effect = [
            _make_sock_with_response(token_bytes),
            _make_sock_with_response(token_bytes),
        ]
        api = WhatsminerMinerAPI("192.168.1.1")
        with mock.patch("tuner_app.miner.whatsminer.time.monotonic") as mock_time:
            mock_time.return_value = 0.0
            api._get_token()
            mock_time.return_value = 24 * 60  # 24 minutes — within 25 min TTL
            api._get_token()
            assert mock_socket.call_count == 1
            mock_time.return_value = 26 * 60  # 26 minutes — past TTL
            api._get_token(force_refresh=True)
            assert mock_socket.call_count == 2


def test_send_raw_method_exists():
    assert hasattr(WhatsminerMinerAPI, "_send_raw")


def test_send_raw_serializes_payload_without_cmd_wrapper():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_sock_factory:
        mock_sock = _make_sock_with_response({"STATUS": "S"})
        mock_sock_factory.return_value = mock_sock
        api = WhatsminerMinerAPI("127.0.0.1")
        api._send_raw({"enc": 1, "data": "abc"})
        sent_bytes = mock_sock.sendall.call_args[0][0]
        payload = json.loads(sent_bytes.decode())
        assert payload == {"enc": 1, "data": "abc"}


def test_send_encrypted_uses_send_raw_no_cmd_wrapper():
    with (
        mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_sock_factory,
        mock.patch.object(WhatsminerMinerAPI, "_get_token", return_value=(b"\x00" * 32, "stub")),
    ):
        mock_sock = _make_sock_with_response({"STATUS": "S"})
        mock_sock_factory.return_value = mock_sock
        api = WhatsminerMinerAPI("127.0.0.1")
        api._send_encrypted("test_cmd", arg="val")
        sent_bytes = mock_sock.sendall.call_args[0][0]
        payload = json.loads(sent_bytes.decode())
        assert set(payload.keys()) == {"enc", "data"}


def test_send_encrypted_inner_payload_includes_token_sign():
    """The inner encrypted JSON for write commands must embed `"token": <sign>` per
    the MicroBT API contract — without it the firmware returns `json token err`."""
    aeskey = b"\x00" * 32
    sign = "expected-sign-value"
    with (
        mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_sock_factory,
        mock.patch.object(WhatsminerMinerAPI, "_get_token", return_value=(aeskey, sign)),
    ):
        mock_sock = _make_sock_with_response({"STATUS": "S"})
        mock_sock_factory.return_value = mock_sock
        api = WhatsminerMinerAPI("127.0.0.1")
        api._send_encrypted("start_mining")
        sent_bytes = mock_sock.sendall.call_args[0][0]
        outer = json.loads(sent_bytes.decode())
        inner_plaintext = _decrypt(outer["data"], aeskey)
        inner = json.loads(inner_plaintext)
        assert inner["cmd"] == "start_mining"
        assert inner["token"] == sign


def test_send_encrypted_retry_rebuilds_inner_payload_with_refreshed_sign():
    """When `_get_token(force_refresh=True)` rotates the sign during a 135-retry,
    the inner payload must be rebuilt with the NEW sign (otherwise the retry
    still carries the stale token and the firmware rejects it again)."""
    old_aeskey = b"\x00" * 32
    new_aeskey = b"\x11" * 32
    api = WhatsminerMinerAPI("192.168.1.1")
    captured_payloads = []

    def fake_send_raw(payload):
        captured_payloads.append(payload)
        if len(captured_payloads) == 1:
            return {"STATUS": "E", "Code": 135}
        return {"STATUS": "S"}

    with (
        mock.patch.object(api, "_send_raw", side_effect=fake_send_raw),
        mock.patch.object(
            api,
            "_get_token",
            side_effect=[(old_aeskey, "old-sign"), (new_aeskey, "new-sign")],
        ),
    ):
        api._send_encrypted("set_target_voltage", voltage=1200)

    assert len(captured_payloads) == 2
    retry_inner = json.loads(_decrypt(captured_payloads[1]["data"], new_aeskey))
    assert retry_inner["token"] == "new-sign"
    assert retry_inner["cmd"] == "set_target_voltage"
    assert retry_inner["voltage"] == 1200


def test_send_plaintext_still_includes_cmd_wrapper():
    with mock.patch("tuner_app.miner.whatsminer.socket.create_connection") as mock_sock_factory:
        mock_sock = _make_sock_with_response({"STATUS": "S"})
        mock_sock_factory.return_value = mock_sock
        api = WhatsminerMinerAPI("127.0.0.1")
        api._send_plaintext("summary")
        sent_bytes = mock_sock.sendall.call_args[0][0]
        payload = json.loads(sent_bytes.decode())
        assert payload["cmd"] == "summary"


def test_send_encrypted_enc_json_load_err_surfaces_password_hint():
    api = WhatsminerMinerAPI("192.168.1.1")
    with (
        mock.patch.object(api, "_send_raw") as mock_send_raw,
        mock.patch.object(api, "_get_token", return_value=(b"\x00" * 32, "sign")),
    ):
        mock_send_raw.return_value = {"STATUS": "E", "Code": 0, "Msg": "enc json load err"}
        with pytest.raises(MinerCommandError) as exc_info:
            api._send_encrypted("start_mining")
        assert "start_mining" in str(exc_info.value)
        assert "PASSWORD" in str(exc_info.value)


def test_send_encrypted_other_error_message_preserved():
    api = WhatsminerMinerAPI("192.168.1.1")
    with (
        mock.patch.object(api, "_send_raw") as mock_send_raw,
        mock.patch.object(api, "_get_token", return_value=(b"\x00" * 32, "sign")),
    ):
        mock_send_raw.return_value = {"STATUS": "E", "Code": 0, "Msg": "some other error"}
        with pytest.raises(MinerCommandError) as exc_info:
            api._send_encrypted("stop_mining")
        assert "some other error" in str(exc_info.value)
        assert "PASSWORD" not in str(exc_info.value)


def test_send_encrypted_retry_failed_with_enc_json_load_err_surfaces_password_hint():
    api = WhatsminerMinerAPI("192.168.1.1")
    with (
        mock.patch.object(api, "_send_raw") as mock_send_raw,
        mock.patch.object(api, "_get_token", return_value=(b"\x00" * 32, "sign")),
    ):
        mock_send_raw.side_effect = [
            {"STATUS": "E", "Code": 135, "Msg": "Token expired"},
            {"STATUS": "E", "Code": 0, "Msg": "enc json load err"},
        ]
        with pytest.raises(MinerCommandError) as exc_info:
            api._send_encrypted("start_mining")
        assert "PASSWORD" in str(exc_info.value)
        assert mock_send_raw.call_count == 2
        assert api._get_token.call_count == 2


def test_send_encrypted_135_token_refresh_still_works():
    api = WhatsminerMinerAPI("192.168.1.1")
    aeskey = b"\xaa" * 32
    success_plaintext = {"STATUS": "S", "Msg": "ok"}
    success_encrypted = _encrypt(json.dumps(success_plaintext), aeskey)
    with (
        mock.patch.object(api, "_send_raw") as mock_send_raw,
        mock.patch.object(api, "_get_token", return_value=(aeskey, "sign")),
    ):
        mock_send_raw.side_effect = [
            {"STATUS": "E", "Code": 135, "Msg": "Token expired"},
            {"data": success_encrypted},
        ]
        result = api._send_encrypted("start_mining")
        assert result["STATUS"] == "S"
        assert result["Msg"] == "ok"
        assert mock_send_raw.call_count == 2
