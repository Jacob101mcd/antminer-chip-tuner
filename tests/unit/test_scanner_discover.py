"""Unit tests for tuner_app.scanner.discover.probe_miner."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from tuner_app.scanner.discover import ProbeResult, probe_miner

_VENDOR_SUMMARY = json.dumps(
    {
        "Status": {"Operating State": "mining"},
        "Network": {"Hostname": "miner-example"},
    }
).encode()

_NO_VENDOR_SUMMARY = json.dumps(
    {
        "Status": {"SomeOtherKey": "value"},
    }
).encode()

_VOLTAGE_OK = json.dumps({"result": True, "data": {}}).encode()


class TestProbeMiner(unittest.TestCase):
    def _call(self, **kwargs):
        defaults = dict(
            ip="192.0.2.5",
            source_ip="",
            api_port=4028,
            passwords=["letmein", "admin"],
            timeout=2.0,
        )
        defaults.update(kwargs)
        return probe_miner(**defaults)

    # (a) /summary non-200 → Bixbit None → LuxOS None → Braiins None → reachable=False
    def test_non_200_summary(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertFalse(result.reachable)
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.password_found)
        self.assertEqual(result.error, "No vendor match")

    # (b) /summary 200 with vendor shape, no passwords → vendor_match=True, password_found=None
    def test_vendor_match_no_password(self):
        def side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], _VENDOR_SUMMARY)
            # /get_voltage calls
            return (401, [], b"unauthorized")

        with patch("tuner_app.scanner.discover.miner_http_request", side_effect=side_effect):
            result = self._call(passwords=[])
        self.assertTrue(result.reachable)
        self.assertTrue(result.vendor_match)
        self.assertIsNone(result.password_found)
        self.assertEqual(result.hostname, "miner-example")

    # (c) first password fails (401), second succeeds (200 JSON) → password_found=second
    def test_second_password_matches(self):
        call_count = [0]

        def side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], _VENDOR_SUMMARY)
            call_count[0] += 1
            if call_count[0] == 1:
                return (401, [], b"unauthorized")
            return (200, [], _VOLTAGE_OK)

        # Patch _fetch_epic_network to None so the new /network fetch
        # introduced post-Unit-3 doesn't perturb the password-call counter.
        with (
            patch("tuner_app.scanner.discover.miner_http_request", side_effect=side_effect),
            patch("tuner_app.scanner.discover._fetch_epic_network", return_value=None),
        ):
            result = self._call(passwords=["wrong", "letmein"])
        self.assertTrue(result.reachable)
        self.assertTrue(result.vendor_match)
        self.assertEqual(result.password_found, "letmein")

    # (d) /summary JSON missing vendor key → Bixbit/LuxOS/Braiins all None → no password attempts.
    def test_no_vendor_key(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (200, [], _NO_VENDOR_SUMMARY)
            result = self._call()
        self.assertFalse(result.reachable)
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.password_found)
        # Only one HTTP call (the /summary GET), no /get_voltage POSTs.
        mock_req.assert_called_once()

    # (e) HTTP raises → Bixbit/LuxOS/Braiins all mocked None → ProbeResult returned
    def test_connection_error_returns_probe_result(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = self._call()
        self.assertIsInstance(result, ProbeResult)
        self.assertFalse(result.reachable)
        self.assertIsNotNone(result.error)

    # firmware_type assertions
    def test_vendor_match_sets_firmware_type_epic(self):
        """vendor_match=True → firmware_type='epic' in ProbeResult."""

        def side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], _VENDOR_SUMMARY)
            return (200, [], _VOLTAGE_OK)

        with patch("tuner_app.scanner.discover.miner_http_request", side_effect=side_effect):
            result = self._call(passwords=["letmein"])
        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "epic")

    def test_no_vendor_match_sets_firmware_type_none(self):
        """vendor_match=False → firmware_type=None in ProbeResult."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (200, [], _NO_VENDOR_SUMMARY)
            result = self._call()
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.firmware_type)

    def test_unreachable_sets_firmware_type_none(self):
        """HTTP error + Bixbit/LuxOS/Braiins None → firmware_type=None in ProbeResult."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = self._call()
        self.assertFalse(result.reachable)
        self.assertIsNone(result.firmware_type)

    def test_vendor_match_no_password_sets_firmware_type_epic(self):
        """vendor_match=True but no password → firmware_type='epic' (vendor is ePIC)."""

        def side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], _VENDOR_SUMMARY)
            return (401, [], b"unauthorized")

        with patch("tuner_app.scanner.discover.miner_http_request", side_effect=side_effect):
            result = self._call(passwords=["wrong"])
        self.assertTrue(result.vendor_match)
        self.assertIsNone(result.password_found)
        self.assertEqual(result.firmware_type, "epic")


class TestProbeMinerBixbit(unittest.TestCase):
    def _call(self, **kwargs):
        defaults = dict(
            ip="192.0.2.5",
            source_ip="",
            api_port=4028,
            passwords=["letmein", "admin"],
            timeout=2.0,
        )
        defaults.update(kwargs)
        return probe_miner(**defaults)

    def test_bixbit_happy_path(self):
        """HTTP /summary 404 → Bixbit TCP returns STATUS dict → firmware_type='bixbit'."""
        bixbit_resp = {"STATUS": "S", "Code": 131, "Msg": "API command OK", "Description": ""}
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=bixbit_resp),
            patch("tuner_app.scanner.discover._validate_bixbit_password", return_value=True),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertTrue(result.reachable)
        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "bixbit")
        self.assertEqual(result.password_found, "letmein")

    def test_bixbit_tcp_timeout(self):
        """HTTP error + Bixbit TCP timeout (None) + LuxOS/Braiins None → vendor_match=False."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = self._call()
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.firmware_type)

    def test_non_vendor_no_password_attempts(self):
        """HTTP 200 non-ePIC + Bixbit/LuxOS/Braiins None → no password attempts."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (200, [], _NO_VENDOR_SUMMARY)
            result = self._call()
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.firmware_type)
        # Only one HTTP call — the /summary GET. No /get_voltage POSTs.
        self.assertEqual(mock_req.call_count, 1)

    def test_probe_bixbit_tcp_rejects_luxos_error_shape(self):
        """Helper _probe_bixbit_tcp must reject LuxOS list-of-dicts STATUS shape and return None."""
        from tuner_app.scanner import discover

        luxos_error_bytes = (
            b'{"STATUS":[{"Code":14,"Description":"LUXminer 2026.4.3.192353-6ab4e5077",'
            b'"Msg":"Invalid command","STATUS":"E","When":1777894261}],"id":1}'
        )
        with patch("socket.create_connection") as mock_conn:
            sock = mock_conn.return_value.__enter__.return_value
            sock.recv.side_effect = [luxos_error_bytes, b""]
            result = discover._probe_bixbit_tcp("1.2.3.4", 4028, 2.0, "")
        self.assertIsNone(result)

    def test_probe_bixbit_tcp_accepts_canonical_string_status(self):
        """Regression guard: canonical Bixbit response with STATUS as string
        "S" must still match."""
        from tuner_app.scanner import discover

        bixbit_bytes = b'{"STATUS":"S","Code":131,"Msg":"API command OK"}'
        with patch("socket.create_connection") as mock_conn:
            sock = mock_conn.return_value.__enter__.return_value
            sock.recv.side_effect = [bixbit_bytes, b""]
            result = discover._probe_bixbit_tcp("1.2.3.4", 4028, 2.0, "")
        self.assertEqual(result, {"STATUS": "S", "Code": 131, "Msg": "API command OK"})


class TestProbeMinerSourceIpPassthrough(unittest.TestCase):
    """Verify that probe_miner forwards source_ip to miner_http_request and _probe_bixbit_tcp."""

    def test_probe_miner_passes_source_ip_through(self):
        """ePIC path: source_ip forwarded to both GET /summary and POST /get_voltage."""

        def side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], _VENDOR_SUMMARY)
            return (200, [], _VOLTAGE_OK)

        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request", side_effect=side_effect
            ) as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None) as mock_bixbit,
        ):
            result = probe_miner(
                "1.2.3.4",
                source_ip="192.168.1.5",
                api_port=4028,
                passwords=["letmein"],
                timeout=2.0,
            )

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "epic")
        # Both miner_http_request calls must carry source_ip="192.168.1.5"
        for c in mock_req.call_args_list:
            self.assertEqual(
                c.kwargs.get("source_ip"),
                "192.168.1.5",
                f"miner_http_request call missing source_ip kwarg: {c}",
            )
        # Bixbit probe must NOT be called on ePIC path
        mock_bixbit.assert_not_called()

    def test_probe_miner_passes_source_ip_to_bixbit(self):
        """Bixbit path: source_ip forwarded to _probe_bixbit_tcp as 4th positional arg."""
        bixbit_resp = {"STATUS": "S", "Code": 131, "Msg": "API command OK", "Description": ""}

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch(
                "tuner_app.scanner.discover._probe_bixbit_tcp", return_value=bixbit_resp
            ) as mock_bixbit,
        ):
            # Raise on HTTP so we fall through to Bixbit
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = probe_miner(
                "1.2.3.4",
                source_ip="192.168.1.5",
                api_port=4028,
                passwords=["letmein"],
                timeout=2.0,
            )

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "bixbit")
        # _probe_bixbit_tcp called with source_ip as 4th positional arg
        mock_bixbit.assert_called_once()
        args, kwargs = mock_bixbit.call_args
        # _probe_bixbit_tcp(ip, api_port, timeout, source_ip) — 4th positional
        self.assertEqual(args[3], "192.168.1.5")


class TestProbeMinerBraiins(unittest.TestCase):
    def _call(self, **kwargs):
        defaults = dict(
            ip="192.0.2.5",
            source_ip="",
            api_port=4028,
            passwords=["letmein", "admin"],
            timeout=2.0,
        )
        defaults.update(kwargs)
        return probe_miner(**defaults)

    def test_braiins_happy_path(self):
        """HTTP /summary 404 → Bixbit None → LuxOS None → Braiins version dict → braiins matched."""
        braiins_resp = {"major": 1, "minor": 3, "patch": 0}
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=braiins_resp),
            patch("tuner_app.scanner.discover._validate_braiins_password", return_value=True),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertTrue(result.reachable)
        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "braiins")
        self.assertEqual(result.password_found, "letmein")
        self.assertIsNone(result.hostname)

    def test_braiins_fingerprint_miss(self):
        """All four probes miss → vendor_match=False, firmware_type=None."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertFalse(result.reachable)
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.firmware_type)
        self.assertEqual(result.error, "No vendor match")

    def test_non_vendor_no_password_attempts_with_braiins(self):
        """HTTP 200 non-ePIC + Bixbit/LuxOS/Braiins None → zero password POST attempts."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (200, [], _NO_VENDOR_SUMMARY)
            result = self._call()
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.firmware_type)
        # Only one HTTP call — the /summary GET. No /get_voltage POSTs.
        self.assertEqual(mock_req.call_count, 1)

    def test_braiins_source_ip_passthrough(self):
        """source_ip forwarded to _probe_braiins_http as 4th positional arg."""
        braiins_resp = {"major": 1, "minor": 3, "patch": 0}

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_braiins_http", return_value=braiins_resp
            ) as mock_braiins,
        ):
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = probe_miner(
                "1.2.3.4",
                source_ip="192.168.1.5",
                api_port=4028,
                passwords=["letmein"],
                timeout=2.0,
            )

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "braiins")
        # _probe_braiins_http called with source_ip as 4th positional arg
        mock_braiins.assert_called_once()
        args, _kwargs = mock_braiins.call_args
        # _probe_braiins_http(ip, api_port, timeout, source_ip) — 4th positional
        self.assertEqual(args[3], "192.168.1.5")

    def test_braiins_non_integer_major_not_matched(self):
        """Response with 'major' as a string (not int) does NOT match Braiins fingerprint."""
        # This simulates a device that happens to return {"major": "1"} — e.g. a web app
        non_braiins_resp = {"major": "1", "minor": "3", "patch": "0"}
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=non_braiins_resp),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        # The mock returns a dict but _probe_braiins_http is itself mocked here;
        # the fingerprint check inside probe_miner re-validates the return value.
        # A dict with string "major" is NOT matched by the isinstance(..., int) check.
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.firmware_type)


class TestProbeBraiinsHttpHelper(unittest.TestCase):
    """Unit tests for _probe_braiins_http in isolation (not via probe_miner)."""

    def setUp(self):
        from tuner_app.scanner.discover import _probe_braiins_http

        self._helper = _probe_braiins_http

    def test_returns_dict_on_valid_response(self):
        version_body = json.dumps({"major": 1, "minor": 3, "patch": 0}).encode()
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_req:
            mock_req.return_value = (200, [], version_body)
            result = self._helper("192.0.2.5", 80, 2.0, "")
        self.assertIsNotNone(result)
        self.assertEqual(result["major"], 1)

    def test_returns_none_on_non_200(self):
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_req:
            mock_req.return_value = (404, [], b"not found")
            result = self._helper("192.0.2.5", 80, 2.0, "")
        self.assertIsNone(result)

    def test_returns_none_on_invalid_json(self):
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_req:
            mock_req.return_value = (200, [], b"not json")
            result = self._helper("192.0.2.5", 80, 2.0, "")
        self.assertIsNone(result)

    def test_returns_none_on_missing_major(self):
        body = json.dumps({"version": "1.3.0"}).encode()
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_req:
            mock_req.return_value = (200, [], body)
            result = self._helper("192.0.2.5", 80, 2.0, "")
        self.assertIsNone(result)

    def test_never_raises_on_connection_error(self):
        """_probe_braiins_http must never raise regardless of the underlying error."""
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_req:
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = self._helper("192.0.2.5", 80, 2.0, "")
        self.assertIsNone(result)

    def test_calls_correct_path(self):
        """Verify the helper calls /api/v1/version/ (not /summary or another path)."""
        version_body = json.dumps({"major": 1, "minor": 3, "patch": 0}).encode()
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_req:
            mock_req.return_value = (200, [], version_body)
            self._helper("192.0.2.5", 80, 2.0, "192.168.1.5")
        call_args = mock_req.call_args
        # positional args: (ip, port, path, ...)
        self.assertEqual(call_args.args[2], "/api/v1/version/")
        self.assertEqual(call_args.kwargs.get("source_ip"), "192.168.1.5")
        self.assertEqual(call_args.kwargs.get("method"), "GET")


class TestProbeMinerLuxOS(unittest.TestCase):
    def _call(self, **kwargs):
        defaults = dict(
            ip="192.0.2.5",
            source_ip="",
            api_port=4028,
            passwords=["letmein", "admin"],
            timeout=2.0,
        )
        defaults.update(kwargs)
        return probe_miner(**defaults)

    def test_luxos_happy_path(self):
        """HTTP /summary 404, Bixbit None, LuxOS TCP matched → firmware_type='luxos'."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp") as mock_bixbit,
            patch("tuner_app.scanner.discover._probe_luxos_tcp") as mock_luxos,
            patch("tuner_app.scanner.discover._validate_luxos_password", return_value=True),
        ):
            mock_req.return_value = (404, [], b"not found")
            mock_bixbit.return_value = None
            mock_luxos.return_value = {
                "STATUS": [
                    {
                        "Code": 22,
                        "Msg": "LUXminer 2024.2.1.0-2024-02-08_07-35-10",
                        "Status": "S",
                    }
                ],
                "VERSION": [
                    {
                        "LUXminer": "2024.2.1.0-2024-02-08_07-35-10",
                        "CGMiner": "4.12.0",
                    }
                ],
            }
            result = self._call()
        self.assertTrue(result.reachable)
        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "luxos")
        self.assertEqual(result.password_found, "letmein")
        self.assertIsNone(result.hostname)

    def test_non_vendor_no_password_attempts_with_luxos(self):
        """HTTP 200 non-ePIC + Bixbit/LuxOS(bad)/Braiins None → zero password POST attempts."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp",
                return_value={"STATUS": []},
            ),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (200, [], b'{"Status": {"SomeOtherKey": "value"}}')
            result = self._call()
        self.assertFalse(result.vendor_match)
        self.assertIsNone(result.firmware_type)
        # Only one HTTP call — the /summary GET. No /get_voltage POSTs.
        self.assertEqual(mock_req.call_count, 1)

    def test_luxos_error_response_not_misidentified_as_bixbit(self):
        """LuxOS error response with STATUS as list-of-dicts must NOT match
        Bixbit fingerprint; LuxOS step downstream must claim it."""
        luxos_error_response = {
            "STATUS": [
                {
                    "Code": 14,
                    "Description": "LUXminer 2026.4.3.192353-6ab4e5077",
                    "Msg": "Invalid command",
                    "STATUS": "E",
                    "When": 1777894261,
                }
            ],
            "id": 1,
        }
        valid_luxos_version_response = {
            "STATUS": [{"Code": 22, "Msg": "LUXminer x", "Status": "S"}],
            "VERSION": [{"LUXminer": "2026.4.3", "CGMiner": "4.12.0"}],
        }
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch(
                "tuner_app.scanner.discover._probe_bixbit_tcp", return_value=luxos_error_response
            ),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp",
                return_value=valid_luxos_version_response,
            ),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "luxos")


class TestProbeMinerSummaryRaw(unittest.TestCase):
    def _call(self, **kwargs):
        defaults = dict(
            ip="192.0.2.5",
            source_ip="",
            api_port=4028,
            passwords=["letmein", "admin"],
            timeout=2.0,
        )
        defaults.update(kwargs)
        return probe_miner(**defaults)

    def test_summary_raw_populated_on_epic_password_found(self):
        def side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], _VENDOR_SUMMARY)
            return (200, [], _VOLTAGE_OK)

        with patch("tuner_app.scanner.discover.miner_http_request", side_effect=side_effect):
            result = self._call()
        self.assertIsInstance(result.summary_raw, dict)
        self.assertEqual(result.summary_raw, json.loads(_VENDOR_SUMMARY))

    def test_summary_raw_populated_on_epic_no_password(self):
        def side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], _VENDOR_SUMMARY)
            return (401, [], b"unauthorized")

        with patch("tuner_app.scanner.discover.miner_http_request", side_effect=side_effect):
            result = self._call(passwords=[])
        self.assertIsNotNone(result.summary_raw)
        self.assertEqual(result.summary_raw, json.loads(_VENDOR_SUMMARY))
        self.assertIsNone(result.password_found)

    def test_summary_raw_populated_on_bixbit_match(self):
        bixbit_resp = {"STATUS": "S", "Code": 131}
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=bixbit_resp),
        ):
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = self._call()
        self.assertIs(result.summary_raw, bixbit_resp)

    def test_summary_raw_none_on_luxos_match(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp",
                return_value={
                    "STATUS": [{"Code": 22, "Msg": "LUXminer 2024.2.1.0", "Status": "S"}],
                    "VERSION": [{"LUXminer": "2024.2.1.0", "CGMiner": "4.12.0"}],
                },
            ),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertIsNone(result.summary_raw)

    def test_summary_raw_none_on_braiins_match(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_braiins_http",
                return_value={"major": 1, "minor": 3, "patch": 0},
            ),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertIsNone(result.summary_raw)

    def test_summary_raw_none_on_no_vendor_match(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.return_value = (200, [], _NO_VENDOR_SUMMARY)
            result = self._call()
        self.assertIsNone(result.summary_raw)

    def test_summary_raw_none_on_unreachable(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = self._call()
        self.assertIsNone(result.summary_raw)


class TestProbeMacFetch(unittest.TestCase):
    """Tests for vendor-API MAC discovery plumbed into probe_miner (Unit 5).

    Tests 1, 5, 6, 7, 8 FAIL on the current codebase because:
      - All _resolve_mac_or_synth call sites pass (ip, source_ip) only —
        no vendor_mac kwarg — so the vendor API MAC is ignored and ARP runs.
      - _fetch_luxos_config does not yet exist in discover.py (AttributeError
        on patch target for cases 6, 7, 8).

    Tests 2, 3, 4, 9 exercise the ARP/synth fallback path which is already
    correct and pass on the current codebase (regression guards).
    """

    _IP = "192.0.2.5"
    _LUXOS_VERSION_RESP = {
        "STATUS": [{"Code": 22, "Msg": "LUXminer 2026.4.3", "Status": "S"}],
        "VERSION": [{"LUXminer": "2026.4.3", "CGMiner": "4.12.0"}],
    }
    _BIXBIT_RESP = {"STATUS": "S", "Code": 131, "Msg": "API command OK", "Description": ""}
    _BRAIINS_RESP = {"major": 1, "minor": 3, "patch": 0}

    def _call(self, **kwargs):
        defaults = dict(
            ip=self._IP,
            source_ip="",
            api_port=4028,
            passwords=["letmein", "admin"],
            timeout=2.0,
        )
        defaults.update(kwargs)
        return probe_miner(**defaults)

    # ── Case 1 ──────────────────────────────────────────────────────────────
    def test_epic_probe_extracts_mac_from_network_endpoint(self):
        """ePIC: MAC from /network short-circuits ARP."""
        epic_summary_no_mac = json.dumps(
            {"Status": {"Operating State": "mining"}, "Hostname": "miner-example"}
        ).encode()

        def http_side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], epic_summary_no_mac)
            return (200, [], _VOLTAGE_OK)

        with (
            patch("tuner_app.scanner.discover.miner_http_request", side_effect=http_side_effect),
            patch(
                "tuner_app.scanner.discover._fetch_epic_network",
                return_value={"dhcp": {"mac_address": "aa:bb:cc:dd:ee:01"}},
            ),
            patch("tuner_app.scanner.discover.resolve_mac") as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "epic")
        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:01")
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_not_called()
        mock_synth.assert_not_called()

    # ── Case 2 ──────────────────────────────────────────────────────────────
    def test_epic_probe_falls_back_to_arp_when_no_mac_in_network(self):
        """ePIC: no MAC in /network → ARP fallback used (regression guard)."""
        epic_summary_no_mac = json.dumps(
            {"Status": {"Operating State": "mining"}, "Hostname": "miner-example"}
        ).encode()

        def http_side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], epic_summary_no_mac)
            return (200, [], _VOLTAGE_OK)

        with (
            patch("tuner_app.scanner.discover.miner_http_request", side_effect=http_side_effect),
            patch("tuner_app.scanner.discover._fetch_epic_network", return_value=None),
            patch(
                "tuner_app.scanner.discover.resolve_mac", return_value="aa:bb:cc:dd:ee:02"
            ) as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:02")
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_called()
        mock_synth.assert_not_called()

    # ── Case 3 ──────────────────────────────────────────────────────────────
    def test_epic_probe_synthesizes_when_no_mac_anywhere(self):
        """ePIC: no MAC in /network or ARP → MAC synthesized."""
        epic_summary_no_mac = json.dumps(
            {"Status": {"Operating State": "mining"}, "Hostname": "miner-example"}
        ).encode()

        def http_side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], epic_summary_no_mac)
            return (200, [], _VOLTAGE_OK)

        with (
            patch("tuner_app.scanner.discover.miner_http_request", side_effect=http_side_effect),
            patch("tuner_app.scanner.discover._fetch_epic_network", return_value=None),
            patch("tuner_app.scanner.discover.resolve_mac", return_value=None),
            patch(
                "tuner_app.scanner.discover.synthesize_mac_id", return_value="syn-aabbccdd"
            ) as mock_synth,
        ):
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.mac, "syn-aabbccdd")
        self.assertTrue(result.id_synthesized)
        mock_synth.assert_called()

    def test_epic_probe_calls_fetch_network_with_correct_args(self):
        """ePIC: _fetch_epic_network called with correct args."""
        epic_summary_no_mac = json.dumps(
            {"Status": {"Operating State": "mining"}, "Hostname": "miner-example"}
        ).encode()

        def http_side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (200, [], epic_summary_no_mac)
            return (200, [], _VOLTAGE_OK)

        with (
            patch("tuner_app.scanner.discover.miner_http_request", side_effect=http_side_effect),
            patch(
                "tuner_app.scanner.discover._fetch_epic_network",
                return_value={"dhcp": {"mac_address": "aa:bb:cc:dd:ee:99"}},
            ) as mock_fetch_network,
        ):
            result = self._call()

        self.assertTrue(result.vendor_match)
        mock_fetch_network.assert_called_once()
        self.assertEqual(mock_fetch_network.call_args.args, (self._IP, 4028, 2.0, ""))

    def test_epic_probe_does_not_fetch_network_when_not_vendor_match(self):
        """ePIC: _fetch_epic_network not called when not ePIC vendor."""

        def http_side_effect(ip, port, path, data=None, method="GET", timeout=15, **kwargs):
            if path == "/summary":
                return (404, [], b"not found")
            return (200, [], _VOLTAGE_OK)

        with (
            patch("tuner_app.scanner.discover.miner_http_request", side_effect=http_side_effect),
            patch(
                "tuner_app.scanner.discover._fetch_epic_network",
                return_value={"dhcp": {"mac_address": "aa:bb:cc:dd:ee:99"}},
            ) as mock_fetch_network,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
        ):
            result = self._call()

        self.assertFalse(result.vendor_match)
        mock_fetch_network.assert_not_called()

    # ── Case 4 ────────────────────────────────────────────────────────────────
    def test_bixbit_probe_no_mac_in_summary_falls_back_to_arp(self):
        """Bixbit: typical summary without MAC field → ARP fallback (regression guard)."""
        # Standard Bixbit response has STATUS as string but no MAC field
        bixbit_no_mac = {"STATUS": "S", "Code": 131, "Msg": "API command OK", "Description": ""}

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=bixbit_no_mac),
            patch(
                "tuner_app.scanner.discover.resolve_mac", return_value="aa:bb:cc:dd:ee:03"
            ) as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "bixbit")
        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:03")
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_called()
        mock_synth.assert_not_called()

    # ── Case 5 ────────────────────────────────────────────────────────────────
    def test_bixbit_probe_uses_mac_from_summary_if_present(self):
        """Bixbit: summary with MAC field → vendor MAC short-circuits ARP."""
        bixbit_with_mac = {
            "STATUS": "S",
            "Code": 131,
            "Msg": "API command OK",
            "Description": "",
            "MAC": "aa:bb:cc:dd:ee:04",
        }

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=bixbit_with_mac),
            patch("tuner_app.scanner.discover.resolve_mac") as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            mock_resolve.return_value = "99:99:99:99:99:99"
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "bixbit")
        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:04")
        self.assertFalse(result.id_synthesized)
        # Vendor MAC short-circuits ARP
        mock_resolve.assert_not_called()
        mock_synth.assert_not_called()

    # ── Case 6 ────────────────────────────────────────────────────────────────
    def test_luxos_probe_extracts_mac_from_config_cmd(self):
        """LuxOS: config cmd returns MACAddr → vendor MAC short-circuits ARP."""
        luxos_config_resp = {"CONFIG": [{"MACAddr": "02:00:5e:10:00:02", "Model": "Antminer S21"}]}

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp", return_value=self._LUXOS_VERSION_RESP
            ),
            patch("tuner_app.scanner.discover._fetch_luxos_config", return_value=luxos_config_resp),
            patch("tuner_app.scanner.discover.resolve_mac") as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            mock_resolve.return_value = "99:99:99:99:99:99"
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "luxos")
        self.assertEqual(result.mac, "02:00:5e:10:00:02")
        self.assertFalse(result.id_synthesized)
        # Vendor MAC short-circuits ARP
        mock_resolve.assert_not_called()
        mock_synth.assert_not_called()

    # ── Case 7 ────────────────────────────────────────────────────────────────
    def test_luxos_probe_falls_back_to_arp_when_config_cmd_fails(self):
        """LuxOS: config cmd returns None → ARP fallback."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp", return_value=self._LUXOS_VERSION_RESP
            ),
            patch("tuner_app.scanner.discover._fetch_luxos_config", return_value=None),
            patch(
                "tuner_app.scanner.discover.resolve_mac", return_value="aa:bb:cc:dd:ee:06"
            ) as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "luxos")
        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:06")
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_called()
        mock_synth.assert_not_called()

    # ── Case 8 ────────────────────────────────────────────────────────────────
    def test_luxos_probe_synthesizes_when_config_and_arp_both_fail(self):
        """LuxOS: config fails AND ARP returns None → synth ID."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp", return_value=self._LUXOS_VERSION_RESP
            ),
            patch("tuner_app.scanner.discover._fetch_luxos_config", return_value=None),
            patch("tuner_app.scanner.discover.resolve_mac", return_value=None),
            patch(
                "tuner_app.scanner.discover.synthesize_mac_id", return_value="syn-luxfallback"
            ) as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "luxos")
        self.assertEqual(result.mac, "syn-luxfallback")
        self.assertTrue(result.id_synthesized)
        mock_synth.assert_called()

    # ── Case 9 ────────────────────────────────────────────────────────────────
    def test_braiins_probe_unchanged(self):
        """Braiins: probe path unchanged by Unit 5 — ARP used, no new helper (regression guard)."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_braiins_http", return_value=self._BRAIINS_RESP
            ),
            patch(
                "tuner_app.scanner.discover.resolve_mac", return_value="aa:bb:cc:dd:ee:07"
            ) as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()

        self.assertTrue(result.vendor_match)
        self.assertEqual(result.firmware_type, "braiins")
        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:07")
        self.assertFalse(result.id_synthesized)
        # Braiins path has no vendor-MAC extraction — ARP must be called
        mock_resolve.assert_called()
        mock_synth.assert_not_called()

    # ── Case 10 ───────────────────────────────────────────────────────────────
    def test_luxos_probe_calls_fetch_config_with_correct_args(self):
        """LuxOS: _fetch_luxos_config is called with (ip, api_port, timeout, source_ip)."""
        luxos_config_resp = {"CONFIG": [{"MACAddr": "02:00:5e:10:00:02"}]}

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp", return_value=self._LUXOS_VERSION_RESP
            ),
            patch(
                "tuner_app.scanner.discover._fetch_luxos_config", return_value=luxos_config_resp
            ) as mock_fetch,
            patch("tuner_app.scanner.discover.resolve_mac", return_value="99:99:99:99:99:99"),
        ):
            mock_req.return_value = (404, [], b"not found")
            self._call(source_ip="10.0.0.1")

        # Verify _fetch_luxos_config received source_ip — critical for multi-homed hosts.
        mock_fetch.assert_called_once_with(self._IP, 4028, 2.0, "10.0.0.1")

    # ── Case 11 ───────────────────────────────────────────────────────────────
    def test_luxos_probe_falls_back_to_arp_when_config_mac_is_all_zeros(self):
        """LuxOS: config returns CONFIG with all-zeros MACAddr → vendor_mac=None → ARP fallback."""
        bad_config = {"CONFIG": [{"MACAddr": "00:00:00:00:00:00"}]}

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp", return_value=self._LUXOS_VERSION_RESP
            ),
            patch("tuner_app.scanner.discover._fetch_luxos_config", return_value=bad_config),
            patch(
                "tuner_app.scanner.discover.resolve_mac", return_value="aa:bb:cc:dd:ee:08"
            ) as mock_resolve,
            patch("tuner_app.scanner.discover.synthesize_mac_id") as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()

        self.assertEqual(result.mac, "aa:bb:cc:dd:ee:08")
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_called()
        mock_synth.assert_not_called()


class TestProbeMinerWhatsminerPasswordValidation(unittest.TestCase):
    def test_whatsminer_probe_first_password_valid(self):
        with (
            patch(
                "tuner_app.scanner.discover._probe_whatsminer_tcp",
                return_value={
                    "STATUS": "S",
                    "Code": 133,
                    "Msg": {"salt": "abc", "newsalt": "def", "time": "100"},
                },
            ),
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch(
                "tuner_app.scanner.discover._validate_whatsminer_password",
                side_effect=lambda ip, api_port, password, salt, timeout, source_ip="": (
                    password == "admin"
                ),
            ),
        ):
            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=4028,
                passwords=["admin", "second"],
                timeout=2.0,
            )

            self.assertEqual(result.password_found, "admin")
            self.assertEqual(result.firmware_type, "whatsminer")
            self.assertTrue(result.vendor_match)

    def test_whatsminer_probe_second_password_valid(self):
        with (
            patch(
                "tuner_app.scanner.discover._probe_whatsminer_tcp",
                return_value={
                    "STATUS": "S",
                    "Code": 133,
                    "Msg": {"salt": "abc", "newsalt": "def", "time": "100"},
                },
            ),
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch(
                "tuner_app.scanner.discover._validate_whatsminer_password",
                side_effect=lambda ip, api_port, password, salt, timeout, source_ip="": (
                    password == "correct"
                ),
            ),
        ):
            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=4028,
                passwords=["wrong", "correct"],
                timeout=2.0,
            )

            self.assertEqual(result.password_found, "correct")

    def test_whatsminer_probe_no_password_matches(self):
        with (
            patch(
                "tuner_app.scanner.discover._probe_whatsminer_tcp",
                return_value={
                    "STATUS": "S",
                    "Code": 133,
                    "Msg": {"salt": "abc", "newsalt": "def", "time": "100"},
                },
            ),
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch("tuner_app.scanner.discover._validate_whatsminer_password", return_value=False),
        ):
            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=4028,
                passwords=["wrong1", "wrong2"],
                timeout=2.0,
            )

            self.assertIsNone(result.password_found)
            self.assertTrue(result.vendor_match)
            self.assertEqual(result.firmware_type, "whatsminer")

    def test_whatsminer_probe_empty_password_list(self):
        with (
            patch(
                "tuner_app.scanner.discover._probe_whatsminer_tcp",
                return_value={
                    "STATUS": "S",
                    "Code": 133,
                    "Msg": {"salt": "abc", "newsalt": "def", "time": "100"},
                },
            ),
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch(
                "tuner_app.scanner.discover._validate_whatsminer_password", return_value=True
            ) as mock_validate,
        ):
            result = probe_miner(
                ip="192.0.2.5", source_ip="", api_port=4028, passwords=[], timeout=2.0
            )

            self.assertIsNone(result.password_found)
            self.assertTrue(result.vendor_match)
            self.assertEqual(mock_validate.call_count, 0)

    def test_whatsminer_probe_no_fingerprint_unchanged(self):
        with (
            patch("tuner_app.scanner.discover._probe_whatsminer_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch(
                "tuner_app.scanner.discover._validate_whatsminer_password", return_value=True
            ) as mock_validate,
        ):
            result = probe_miner(
                ip="192.0.2.5", source_ip="", api_port=4028, passwords=["whatever"], timeout=2.0
            )

            self.assertFalse(result.reachable)
            self.assertFalse(result.vendor_match)
            self.assertEqual(mock_validate.call_count, 0)

    def test_validate_whatsminer_password_returns_true_on_decryptable_response(self):
        from tuner_app.miner.whatsminer import _compute_aeskey, _encrypt

        aeskey = _compute_aeskey("admin", "abcdefgh")
        plaintext = {"STATUS": "S"}
        encrypted = _encrypt(json.dumps(plaintext), aeskey)
        response_data = {"data": encrypted}
        response_bytes = json.dumps(response_data).encode()

        with patch("tuner_app.scanner.discover.socket.create_connection") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.__enter__.return_value = mock_sock
            mock_sock.__exit__.return_value = False
            mock_sock.recv.side_effect = [response_bytes, b""]
            mock_sock.sendall.return_value = None
            mock_socket.return_value = mock_sock

            from tuner_app.scanner.discover import _validate_whatsminer_password

            result = _validate_whatsminer_password(
                ip="1.2.3.4", api_port=4028, password="admin", salt="abcdefgh", timeout=2.0
            )

            self.assertTrue(result)

    def test_validate_whatsminer_password_returns_false_on_enc_json_load_err(self):
        response_bytes = json.dumps({"STATUS": "E", "Msg": "enc json load err"}).encode()

        with patch("tuner_app.scanner.discover.socket.create_connection") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.__enter__.return_value = mock_sock
            mock_sock.__exit__.return_value = False
            mock_sock.recv.side_effect = [response_bytes, b""]
            mock_sock.sendall.return_value = None
            mock_socket.return_value = mock_sock

            from tuner_app.scanner.discover import _validate_whatsminer_password

            result = _validate_whatsminer_password(
                ip="1.2.3.4", api_port=4028, password="admin", salt="abcdefgh", timeout=2.0
            )

            self.assertFalse(result)

    def test_validate_whatsminer_password_returns_false_on_socket_error(self):
        with patch("tuner_app.scanner.discover.socket.create_connection") as mock_socket:
            mock_socket.side_effect = TimeoutError()

            from tuner_app.scanner.discover import _validate_whatsminer_password

            result = _validate_whatsminer_password(
                ip="1.2.3.4", api_port=4028, password="admin", salt="abcdefgh", timeout=2.0
            )

            self.assertFalse(result)

    def test_validate_whatsminer_password_returns_false_on_garbage_response(self):
        with patch("tuner_app.scanner.discover.socket.create_connection") as mock_socket:
            mock_sock = MagicMock()
            mock_sock.__enter__.return_value = mock_sock
            mock_sock.__exit__.return_value = False
            mock_sock.recv.side_effect = [b"\x00\x01garbage", b""]
            mock_sock.sendall.return_value = None
            mock_socket.return_value = mock_sock

            from tuner_app.scanner.discover import _validate_whatsminer_password

            result = _validate_whatsminer_password(
                ip="1.2.3.4",
                api_port=4028,
                password="admin",
                salt="abcdefgh",
                timeout=2.0,
            )

            self.assertFalse(result)


class TestProbeMinerBixbitPasswordValidation(unittest.TestCase):
    def test_validate_bixbit_password_returns_true_on_success(self):
        mock_socket = MagicMock()
        mock_socket.recv.side_effect = [json.dumps({"STATUS": "S", "Msg": "ok"}).encode(), b""]
        mock_socket.sendall.return_value = None
        mock_socket.__enter__.return_value = mock_socket
        mock_socket.__exit__.return_value = False

        with patch("tuner_app.scanner.discover.socket.create_connection", return_value=mock_socket):
            from tuner_app.scanner.discover import _validate_bixbit_password

            result = _validate_bixbit_password(
                ip="1.2.3.4", api_port=4028, password="admin", timeout=2.0
            )

            self.assertTrue(result)

    def test_validate_bixbit_password_returns_false_on_socket_error(self):
        with patch("tuner_app.scanner.discover.socket.create_connection") as mock_socket:
            mock_socket.side_effect = TimeoutError()

            from tuner_app.scanner.discover import _validate_bixbit_password

            result = _validate_bixbit_password(
                ip="1.2.3.4", api_port=4028, password="admin", timeout=2.0
            )

            self.assertFalse(result)

    def test_validate_bixbit_password_returns_false_on_garbage_response(self):
        mock_socket = MagicMock()
        mock_socket.recv.side_effect = [b"\x00\x01garbage", b""]
        mock_socket.__enter__.return_value = mock_socket
        mock_socket.__exit__.return_value = False

        with patch("tuner_app.scanner.discover.socket.create_connection", return_value=mock_socket):
            from tuner_app.scanner.discover import _validate_bixbit_password

            result = _validate_bixbit_password(
                ip="1.2.3.4", api_port=4028, password="admin", timeout=2.0
            )

            self.assertFalse(result)

    def test_bixbit_probe_first_password_valid(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch("tuner_app.scanner.discover._probe_whatsminer_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_bixbit_tcp",
                return_value={"STATUS": "S", "Msg": "ok"},
            ),
            patch("tuner_app.scanner.discover._validate_bixbit_password") as mock_validate,
        ):
            mock_validate.side_effect = lambda ip, api_port, password, timeout, source_ip="": (
                password == "correct"
            )

            from tuner_app.scanner.discover import probe_miner

            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=4028,
                passwords=["correct", "wrong"],
                timeout=2.0,
            )

            self.assertEqual(result.password_found, "correct")
            self.assertTrue(result.vendor_match)
            self.assertEqual(result.firmware_type, "bixbit")

    def test_bixbit_probe_no_password_matches(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch("tuner_app.scanner.discover._probe_whatsminer_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_bixbit_tcp",
                return_value={"STATUS": "S", "Msg": "ok"},
            ),
            patch("tuner_app.scanner.discover._validate_bixbit_password", return_value=False),
        ):
            from tuner_app.scanner.discover import probe_miner

            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=4028,
                passwords=["wrong1", "wrong2"],
                timeout=2.0,
            )

            self.assertIsNone(result.password_found)
            self.assertTrue(result.vendor_match)
            self.assertEqual(result.firmware_type, "bixbit")


class TestProbeMinerLuxosPasswordValidation(unittest.TestCase):
    def test_validate_luxos_password_returns_true_on_success(self):
        mock_sock = MagicMock()
        mock_sock.__enter__.return_value = mock_sock
        mock_sock.__exit__.return_value = False
        mock_sock.recv.side_effect = [
            json.dumps(
                {
                    "STATUS": [{"Code": 7, "Msg": "Logon successful", "STATUS": "S"}],
                    "SESSION": [{"SessionID": "abc123"}],
                }
            ).encode(),
            b"",
        ]
        with patch("tuner_app.scanner.discover.socket.create_connection", return_value=mock_sock):
            from tuner_app.scanner.discover import _validate_luxos_password

            result = _validate_luxos_password(
                ip="1.2.3.4", api_port=4028, password="admin", timeout=2.0
            )
            self.assertTrue(result)

    def test_validate_luxos_password_returns_false_on_socket_error(self):
        with patch(
            "tuner_app.scanner.discover.socket.create_connection", side_effect=TimeoutError()
        ):
            from tuner_app.scanner.discover import _validate_luxos_password

            result = _validate_luxos_password(
                ip="1.2.3.4", api_port=4028, password="admin", timeout=2.0
            )
            self.assertFalse(result)

    def test_validate_luxos_password_returns_false_on_garbage_response(self):
        mock_sock = MagicMock()
        mock_sock.__enter__.return_value = mock_sock
        mock_sock.__exit__.return_value = False
        mock_sock.recv.side_effect = [b"\x00\x01garbage", b""]
        with patch("tuner_app.scanner.discover.socket.create_connection", return_value=mock_sock):
            from tuner_app.scanner.discover import _validate_luxos_password

            result = _validate_luxos_password(
                ip="1.2.3.4", api_port=4028, password="admin", timeout=2.0
            )
            self.assertFalse(result)

    def test_luxos_probe_first_password_valid(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch("tuner_app.scanner.discover._probe_whatsminer_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp",
                return_value={
                    "STATUS": [{"Code": 22, "Msg": "...", "STATUS": "S"}],
                    "VERSION": [{"LUXminer": "2026.4.3", "API": "3.7"}],
                },
            ),
            patch("tuner_app.scanner.discover._fetch_luxos_config", return_value=None),
            patch(
                "tuner_app.scanner.discover._validate_luxos_password",
                side_effect=lambda ip, api_port, password, timeout, source_ip="": (
                    password == "correct"
                ),
            ),
        ):
            from tuner_app.scanner.discover import probe_miner

            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=4028,
                passwords=["correct", "wrong"],
                timeout=2.0,
            )
            self.assertEqual(result.password_found, "correct")
            self.assertTrue(result.vendor_match)
            self.assertEqual(result.firmware_type, "luxos")

    def test_luxos_probe_no_password_matches(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch("tuner_app.scanner.discover._probe_whatsminer_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_luxos_tcp",
                return_value={
                    "STATUS": [{"Code": 22, "Msg": "...", "STATUS": "S"}],
                    "VERSION": [{"LUXminer": "2026.4.3", "API": "3.7"}],
                },
            ),
            patch("tuner_app.scanner.discover._fetch_luxos_config", return_value=None),
            patch("tuner_app.scanner.discover._validate_luxos_password", return_value=False),
        ):
            from tuner_app.scanner.discover import probe_miner

            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=4028,
                passwords=["wrong1", "wrong2"],
                timeout=2.0,
            )
            self.assertIsNone(result.password_found)
            self.assertTrue(result.vendor_match)
            self.assertEqual(result.firmware_type, "luxos")


class TestProbeMinerBraiinsPasswordValidation(unittest.TestCase):
    def test_validate_braiins_password_returns_true_on_success(self):
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_http:
            mock_http.return_value = (
                200,
                [],
                json.dumps({"token": "abc.def.ghi", "timeout_s": 3600}).encode(),
            )
            from tuner_app.scanner.discover import _validate_braiins_password

            result = _validate_braiins_password(
                ip="1.2.3.4", api_port=80, password="root", timeout=2.0
            )
            self.assertTrue(result)

    def test_validate_braiins_password_returns_false_on_connection_error(self):
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_http:
            mock_http.side_effect = ConnectionRefusedError()
            from tuner_app.scanner.discover import _validate_braiins_password

            result = _validate_braiins_password(
                ip="1.2.3.4", api_port=80, password="root", timeout=2.0
            )
            self.assertFalse(result)

    def test_validate_braiins_password_returns_false_on_non_200(self):
        with patch("tuner_app.scanner.discover.miner_http_request") as mock_http:
            mock_http.return_value = (401, [], b'{"error":"unauthorized"}')
            from tuner_app.scanner.discover import _validate_braiins_password

            result = _validate_braiins_password(
                ip="1.2.3.4", api_port=80, password="root", timeout=2.0
            )
            self.assertFalse(result)

    def test_braiins_probe_first_password_valid(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch("tuner_app.scanner.discover._probe_whatsminer_tcp") as mock_whatsminer,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp") as mock_bixbit,
            patch("tuner_app.scanner.discover._probe_luxos_tcp") as mock_luxos,
            patch("tuner_app.scanner.discover._probe_braiins_http") as mock_braiins,
            patch("tuner_app.scanner.discover._validate_braiins_password") as mock_validate,
        ):
            mock_whatsminer.return_value = None
            mock_bixbit.return_value = None
            mock_luxos.return_value = None
            mock_braiins.return_value = {"major": 1, "minor": 3, "patch": 0}
            mock_validate.side_effect = lambda ip, api_port, password, timeout, source_ip="": (
                password == "correct"
            )
            from tuner_app.scanner.discover import probe_miner

            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=80,
                passwords=["correct", "wrong"],
                timeout=2.0,
            )
            self.assertEqual(result.password_found, "correct")
            self.assertTrue(result.vendor_match)
            self.assertEqual(result.firmware_type, "braiins")

    def test_braiins_probe_no_password_matches(self):
        with (
            patch("tuner_app.scanner.discover.miner_http_request", return_value=(404, [], b"")),
            patch("tuner_app.scanner.discover._probe_whatsminer_tcp") as mock_whatsminer,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp") as mock_bixbit,
            patch("tuner_app.scanner.discover._probe_luxos_tcp") as mock_luxos,
            patch("tuner_app.scanner.discover._probe_braiins_http") as mock_braiins,
            patch("tuner_app.scanner.discover._validate_braiins_password") as mock_validate,
        ):
            mock_whatsminer.return_value = None
            mock_bixbit.return_value = None
            mock_luxos.return_value = None
            mock_braiins.return_value = {"major": 1, "minor": 3, "patch": 0}
            mock_validate.return_value = False
            from tuner_app.scanner.discover import probe_miner

            result = probe_miner(
                ip="192.0.2.5",
                source_ip="",
                api_port=80,
                passwords=["wrong1", "wrong2"],
                timeout=2.0,
            )
            self.assertIsNone(result.password_found)
            self.assertTrue(result.vendor_match)
            self.assertEqual(result.firmware_type, "braiins")


if __name__ == "__main__":
    unittest.main(verbosity=2)
