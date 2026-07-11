# tests/unit/test_scanner_discover_whatsminer.py
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from tuner_app.scanner.discover import _probe_whatsminer_tcp, probe_miner


def _make_sock_with_response(response_dict):
    mock_sock = MagicMock()
    data = json.dumps(response_dict).encode()
    mock_sock.recv.side_effect = [data, b""]
    mock_sock.__enter__ = lambda s: mock_sock
    mock_sock.__exit__ = MagicMock(return_value=False)
    return mock_sock


class TestProbeWhatsminerTCP(unittest.TestCase):
    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_happy_path(self, mock_connect):
        response = {
            "STATUS": "S",
            "Code": 133,
            "Msg": {"salt": "abc", "newsalt": "def", "time": "123"},
        }
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertEqual(result, response)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_status_e(self, mock_connect):
        response = {"STATUS": "E", "Code": 133, "Msg": {}}
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_code_not_133(self, mock_connect):
        response = {"STATUS": "S", "Code": 200, "Msg": {}}
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_msg_not_dict(self, mock_connect):
        response = {"STATUS": "S", "Code": 133, "Msg": "not_a_dict"}
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_missing_salt(self, mock_connect):
        response = {"STATUS": "S", "Code": 133, "Msg": {"newsalt": "def", "time": "123"}}
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_missing_newsalt(self, mock_connect):
        response = {"STATUS": "S", "Code": 133, "Msg": {"salt": "abc", "time": "123"}}
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_missing_time(self, mock_connect):
        response = {"STATUS": "S", "Code": 133, "Msg": {"salt": "abc", "newsalt": "def"}}
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_socket_timeout(self, mock_connect):
        mock_connect.side_effect = TimeoutError()

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_connection_refused(self, mock_connect):
        mock_connect.side_effect = ConnectionRefusedError()

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_malformed_json(self, mock_connect):
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = [b"invalid json", b""]
        mock_sock.__enter__ = lambda s: mock_sock
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_connect.return_value = mock_sock

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_arbitrary_exception(self, mock_connect):
        mock_connect.side_effect = RuntimeError("Something went wrong")

        result = _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0)

        self.assertIsNone(result)

    @patch("tuner_app.scanner.discover.socket.create_connection")
    def test_source_ip_forwarded(self, mock_connect):
        response = {
            "STATUS": "S",
            "Code": 133,
            "Msg": {"salt": "abc", "newsalt": "def", "time": "123"},
        }
        mock_sock = _make_sock_with_response(response)
        mock_connect.return_value = mock_sock

        _probe_whatsminer_tcp("192.168.1.1", 4028, 2.0, source_ip="10.0.0.1")

        mock_connect.assert_called_once_with(
            ("192.168.1.1", 4028),
            timeout=2.0,
            source_address=("10.0.0.1", 0),
        )


class TestProbeMinerWhatsminerOrdering(unittest.TestCase):
    @patch("tuner_app.scanner.discover._validate_whatsminer_password", return_value=True)
    @patch("tuner_app.scanner.discover._resolve_mac_or_synth")
    @patch("tuner_app.scanner.discover._probe_braiins_http")
    @patch("tuner_app.scanner.discover._probe_luxos_tcp")
    @patch("tuner_app.scanner.discover._probe_bixbit_tcp")
    @patch("tuner_app.scanner.discover._probe_whatsminer_tcp")
    @patch("tuner_app.scanner.discover.miner_http_request")
    def test_whatsminer_probed_first(
        self,
        mock_http,
        mock_whatsminer,
        mock_bixbit,
        mock_luxos,
        mock_braiins,
        mock_resolve_mac,
        mock_validate,
    ):
        mock_http.return_value = (404, [], b"not found")
        mock_whatsminer.return_value = {
            "STATUS": "S",
            "Code": 133,
            "Msg": {"salt": "abc", "newsalt": "def", "time": "123"},
        }
        mock_bixbit.return_value = None
        mock_luxos.return_value = None
        mock_braiins.return_value = None
        mock_resolve_mac.return_value = ("aa:bb:cc:dd:ee:ff", False)

        result = probe_miner(
            "192.168.1.1",
            source_ip="",
            api_port=4028,
            passwords=["pass123"],
            timeout=2.0,
        )

        self.assertEqual(result.firmware_type, "whatsminer")
        self.assertTrue(result.vendor_match)
        self.assertTrue(result.reachable)
        self.assertEqual(result.password_found, "pass123")
        self.assertEqual(result.summary_raw, mock_whatsminer.return_value)
        mock_luxos.assert_not_called()

    @patch("tuner_app.scanner.discover._validate_whatsminer_password", return_value=True)
    @patch("tuner_app.scanner.discover._resolve_mac_or_synth")
    @patch("tuner_app.scanner.discover._probe_braiins_http")
    @patch("tuner_app.scanner.discover._probe_luxos_tcp")
    @patch("tuner_app.scanner.discover._probe_bixbit_tcp")
    @patch("tuner_app.scanner.discover._probe_whatsminer_tcp")
    @patch("tuner_app.scanner.discover.miner_http_request")
    def test_only_whatsminer_matches(
        self,
        mock_http,
        mock_whatsminer,
        mock_bixbit,
        mock_luxos,
        mock_braiins,
        mock_resolve_mac,
        mock_validate,
    ):
        mock_http.return_value = (404, [], b"not found")
        mock_whatsminer.return_value = {
            "STATUS": "S",
            "Code": 133,
            "Msg": {"salt": "abc", "newsalt": "def", "time": "123"},
        }
        mock_bixbit.return_value = None
        mock_luxos.return_value = None
        mock_braiins.return_value = None
        mock_resolve_mac.return_value = ("aa:bb:cc:dd:ee:ff", False)

        result = probe_miner(
            "192.168.1.1",
            source_ip="",
            api_port=4028,
            passwords=["pass123"],
            timeout=2.0,
        )

        self.assertEqual(result.firmware_type, "whatsminer")
        self.assertTrue(result.vendor_match)
        self.assertTrue(result.reachable)
        self.assertEqual(result.password_found, "pass123")
        self.assertEqual(result.summary_raw, mock_whatsminer.return_value)
