"""Unit tests for MAC resolution integration in tuner_app.scanner.discover.

Tests verify that:
  - ProbeResult carries two new fields: `mac` (str | None) and `id_synthesized` (bool).
  - probe_miner calls resolve_mac after any successful vendor fingerprint match.
  - When resolve_mac returns None the synthesize_mac_id fallback fires and
    id_synthesized is set True.
  - The vendor-match-but-no-password-worked ePIC path still resolves a MAC.
  - No MAC resolution happens when the IP didn't fingerprint as any known vendor.
  - resolve_mac is invoked with both ip and source_ip keyword args.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

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

_IP = "192.0.2.5"
_SOURCE_IP = ""
_MAC = "aa:bb:cc:dd:ee:ff"
_SYN_MAC = "syn-fake-id"

_PATCH_RESOLVE = "tuner_app.scanner.discover.resolve_mac"
_PATCH_SYNTH = "tuner_app.scanner.discover.synthesize_mac_id"


class _MacResolutionBase(unittest.TestCase):
    """Shared _call helper used by all test classes in this file."""

    def _call(self, **kwargs):
        defaults = dict(
            ip=_IP,
            source_ip=_SOURCE_IP,
            api_port=4028,
            passwords=["letmein"],
            timeout=2.0,
        )
        defaults.update(kwargs)
        return probe_miner(**defaults)


class TestProbeResultFieldDefaults(_MacResolutionBase):
    """ProbeResult dataclass default values for the two new fields."""

    def test_mac_defaults_to_none(self):
        """ProbeResult constructed without mac kwarg defaults mac to None."""
        result = ProbeResult(
            ip="x",
            reachable=False,
            vendor_match=False,
            password_found=None,
            hostname=None,
            error=None,
            firmware_type=None,
        )
        self.assertIsNone(result.mac)

    def test_id_synthesized_defaults_to_false(self):
        """ProbeResult constructed without id_synthesized kwarg defaults to False."""
        result = ProbeResult(
            ip="x",
            reachable=False,
            vendor_match=False,
            password_found=None,
            hostname=None,
            error=None,
            firmware_type=None,
        )
        self.assertFalse(result.id_synthesized)

    def test_mac_can_be_set_explicitly(self):
        """ProbeResult accepts mac as a keyword arg and stores it."""
        result = ProbeResult(
            ip="x",
            reachable=True,
            vendor_match=True,
            password_found="pw",
            hostname=None,
            error=None,
            firmware_type="epic",
            mac=_MAC,
            id_synthesized=False,
        )
        self.assertEqual(result.mac, _MAC)
        self.assertFalse(result.id_synthesized)

    def test_id_synthesized_can_be_set_true(self):
        """ProbeResult accepts id_synthesized=True and stores it."""
        result = ProbeResult(
            ip="x",
            reachable=True,
            vendor_match=True,
            password_found=None,
            hostname=None,
            error=None,
            firmware_type="epic",
            mac=_SYN_MAC,
            id_synthesized=True,
        )
        self.assertTrue(result.id_synthesized)


class TestEpicPasswordFoundArpResolves(_MacResolutionBase):
    """ePIC fingerprint, password accepted, ARP returns a real MAC."""

    def _epic_side_effect(self, ip, port, path, data=None, method="GET", timeout=15, **kwargs):
        if path == "/summary":
            return (200, [], _VENDOR_SUMMARY)
        return (200, [], _VOLTAGE_OK)

    def test_mac_set_from_resolve_mac(self):
        """When resolve_mac returns a non-None string, ProbeResult.mac holds that string."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC),
            patch(_PATCH_SYNTH) as mock_synth,
        ):
            result = self._call()
        self.assertEqual(result.mac, _MAC)
        mock_synth.assert_not_called()

    def test_id_synthesized_false_when_arp_resolves(self):
        """id_synthesized is False when resolve_mac returned a real MAC."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC),
            patch(_PATCH_SYNTH),
        ):
            result = self._call()
        self.assertFalse(result.id_synthesized)

    def test_resolve_mac_called_once(self):
        """resolve_mac is called exactly once per probe on success."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC) as mock_resolve,
            patch(_PATCH_SYNTH),
        ):
            self._call()
        mock_resolve.assert_called_once()


class TestEpicPasswordFoundArpFails(_MacResolutionBase):
    """ePIC fingerprint, password accepted, ARP returns None → synthesize fallback."""

    def _epic_side_effect(self, ip, port, path, data=None, method="GET", timeout=15, **kwargs):
        if path == "/summary":
            return (200, [], _VENDOR_SUMMARY)
        return (200, [], _VOLTAGE_OK)

    def test_mac_set_from_synthesize_when_arp_returns_none(self):
        """When resolve_mac returns None, ProbeResult.mac holds the synthesized id."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=None),
            patch(_PATCH_SYNTH, return_value=_SYN_MAC) as mock_synth,
        ):
            result = self._call()
        self.assertEqual(result.mac, _SYN_MAC)
        mock_synth.assert_called_once_with(_IP)

    def test_id_synthesized_true_when_arp_fails(self):
        """id_synthesized is True when the fallback synthesizer was used."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=None),
            patch(_PATCH_SYNTH, return_value=_SYN_MAC),
        ):
            result = self._call()
        self.assertTrue(result.id_synthesized)


class TestEpicVendorMatchNoPassword(_MacResolutionBase):
    """ePIC fingerprint matched but no password worked — MAC still resolved."""

    def _epic_pw_fail_side_effect(
        self, ip, port, path, data=None, method="GET", timeout=15, **kwargs
    ):
        if path == "/summary":
            return (200, [], _VENDOR_SUMMARY)
        return (401, [], b"unauthorized")

    def test_mac_populated_on_vendor_match_no_password(self):
        """Vendor-match-but-no-password path still calls resolve_mac and sets mac."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_pw_fail_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC) as mock_resolve,
            patch(_PATCH_SYNTH),
        ):
            result = self._call(passwords=["wrong"])
        self.assertEqual(result.mac, _MAC)
        mock_resolve.assert_called_once()

    def test_id_synthesized_false_when_arp_resolves_no_password_path(self):
        """id_synthesized is False when resolve_mac returns a real MAC (no-pw path)."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_pw_fail_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC),
            patch(_PATCH_SYNTH),
        ):
            result = self._call(passwords=["wrong"])
        self.assertFalse(result.id_synthesized)

    def test_synth_used_when_arp_fails_no_password_path(self):
        """Fallback synthesizer is called when resolve_mac returns None on no-pw path."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_pw_fail_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=None),
            patch(_PATCH_SYNTH, return_value=_SYN_MAC) as mock_synth,
        ):
            result = self._call(passwords=["wrong"])
        self.assertEqual(result.mac, _SYN_MAC)
        self.assertTrue(result.id_synthesized)
        mock_synth.assert_called_once_with(_IP)


class TestBixbitMacResolution(_MacResolutionBase):
    """Bixbit fingerprint matched — MAC resolution fires via same ARP path."""

    _BIXBIT_RESP = {"STATUS": "S", "Power": 1000}

    def test_mac_set_on_bixbit_match(self):
        """Bixbit fingerprint match resolves MAC and sets ProbeResult.mac."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=self._BIXBIT_RESP),
            patch(_PATCH_RESOLVE, return_value=_MAC) as mock_resolve,
            patch(_PATCH_SYNTH),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertEqual(result.mac, _MAC)
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_called_once()

    def test_synth_used_when_arp_fails_on_bixbit(self):
        """Synth fallback fires when resolve_mac returns None on the Bixbit path."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=self._BIXBIT_RESP),
            patch(_PATCH_RESOLVE, return_value=None),
            patch(_PATCH_SYNTH, return_value=_SYN_MAC) as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertEqual(result.mac, _SYN_MAC)
        self.assertTrue(result.id_synthesized)
        mock_synth.assert_called_once_with(_IP)


class TestLuxosMacResolution(_MacResolutionBase):
    """LuxOS fingerprint matched — MAC resolution fires via same ARP path."""

    _LUXOS_RESP = {
        "STATUS": [{"Code": 22, "Msg": "LUXminer 2024.2.1.0", "Status": "S"}],
        "VERSION": [{"LUXminer": "2024.2.1.0", "CGMiner": "4.12.0"}],
    }

    def test_mac_set_on_luxos_match(self):
        """LuxOS fingerprint match resolves MAC and sets ProbeResult.mac."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=self._LUXOS_RESP),
            patch(_PATCH_RESOLVE, return_value=_MAC) as mock_resolve,
            patch(_PATCH_SYNTH),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertEqual(result.mac, _MAC)
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_called_once()

    def test_synth_used_when_arp_fails_on_luxos(self):
        """Synth fallback fires when resolve_mac returns None on the LuxOS path."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=self._LUXOS_RESP),
            patch(_PATCH_RESOLVE, return_value=None),
            patch(_PATCH_SYNTH, return_value=_SYN_MAC) as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertEqual(result.mac, _SYN_MAC)
        self.assertTrue(result.id_synthesized)
        mock_synth.assert_called_once_with(_IP)


class TestBraiinsMacResolution(_MacResolutionBase):
    """Braiins fingerprint matched — MAC resolution fires via same ARP path."""

    _BRAIINS_RESP = {"major": 1, "minor": 0, "patch": 0}

    def test_mac_set_on_braiins_match(self):
        """Braiins fingerprint match resolves MAC and sets ProbeResult.mac."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_braiins_http", return_value=self._BRAIINS_RESP
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC) as mock_resolve,
            patch(_PATCH_SYNTH),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertEqual(result.mac, _MAC)
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_called_once()

    def test_synth_used_when_arp_fails_on_braiins(self):
        """Synth fallback fires when resolve_mac returns None on the Braiins path."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch(
                "tuner_app.scanner.discover._probe_braiins_http", return_value=self._BRAIINS_RESP
            ),
            patch(_PATCH_RESOLVE, return_value=None),
            patch(_PATCH_SYNTH, return_value=_SYN_MAC) as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertEqual(result.mac, _SYN_MAC)
        self.assertTrue(result.id_synthesized)
        mock_synth.assert_called_once_with(_IP)


class TestExceptionPathMacFields(_MacResolutionBase):
    """Outer exception path in probe_miner leaves mac=None and id_synthesized=False."""

    def test_mac_none_on_outer_exception(self):
        """When probe_miner catches an outer exception, result.mac is None."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch(_PATCH_RESOLVE) as mock_resolve,
            patch(_PATCH_SYNTH) as mock_synth,
        ):
            # Force an outer-loop exception by raising from a non-suppressed code path.
            # _probe_braiins_http is wrapped in try/except internally; raising from
            # miner_http_request triggers the outer except in probe_miner.
            mock_req.side_effect = ConnectionRefusedError("refused")
            result = self._call()
        # On exception, falling through means no vendor matched.
        # mac stays None and id_synthesized stays False.
        self.assertIsNone(result.mac)
        self.assertFalse(result.id_synthesized)
        # resolve_mac/synthesize must NOT be called on this path.
        mock_resolve.assert_not_called()
        mock_synth.assert_not_called()

    def test_result_still_returned_on_outer_exception(self):
        """probe_miner still returns a ProbeResult (never raises) even on internal errors."""

        # Generate a runtime error that would propagate past inner try/except blocks
        # by patching one of the helper functions to raise inside the success path.
        def boom(*args, **kwargs):
            raise RuntimeError("unexpected internal error")

        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", side_effect=boom),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch(_PATCH_RESOLVE),
            patch(_PATCH_SYNTH),
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertIsInstance(result, ProbeResult)
        self.assertFalse(result.reachable)
        self.assertIsNone(result.mac)
        self.assertFalse(result.id_synthesized)


class TestNoVendorMatchMacNotResolved(_MacResolutionBase):
    """When no vendor fingerprinted the IP, resolve_mac must not be called."""

    def test_mac_none_on_no_vendor_match(self):
        """result.mac is None when no vendor fingerprinted the IP."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch(_PATCH_RESOLVE) as mock_resolve,
            patch(_PATCH_SYNTH) as mock_synth,
        ):
            mock_req.return_value = (200, [], _NO_VENDOR_SUMMARY)
            result = self._call()
        self.assertIsNone(result.mac)
        mock_resolve.assert_not_called()
        mock_synth.assert_not_called()

    def test_id_synthesized_false_on_no_vendor_match(self):
        """id_synthesized is False when no vendor fingerprinted the IP."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch(_PATCH_RESOLVE),
            patch(_PATCH_SYNTH),
        ):
            mock_req.return_value = (200, [], _NO_VENDOR_SUMMARY)
            result = self._call()
        self.assertFalse(result.id_synthesized)

    def test_non_200_no_vendor_match_mac_none(self):
        """All probes miss on non-200 HTTP — result.mac is None."""
        with (
            patch("tuner_app.scanner.discover.miner_http_request") as mock_req,
            patch("tuner_app.scanner.discover._probe_bixbit_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_luxos_tcp", return_value=None),
            patch("tuner_app.scanner.discover._probe_braiins_http", return_value=None),
            patch(_PATCH_RESOLVE) as mock_resolve,
            patch(_PATCH_SYNTH) as mock_synth,
        ):
            mock_req.return_value = (404, [], b"not found")
            result = self._call()
        self.assertIsNone(result.mac)
        self.assertFalse(result.id_synthesized)
        mock_resolve.assert_not_called()
        mock_synth.assert_not_called()


class TestResolveMacCallSignature(_MacResolutionBase):
    """resolve_mac is invoked with ip and source_ip as kwargs."""

    def _epic_side_effect(self, ip, port, path, data=None, method="GET", timeout=15, **kwargs):
        if path == "/summary":
            return (200, [], _VENDOR_SUMMARY)
        return (200, [], _VOLTAGE_OK)

    def test_resolve_mac_called_with_ip_and_source_ip(self):
        """resolve_mac(ip, source_ip=source_ip) is the expected call signature."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC) as mock_resolve,
            patch(_PATCH_SYNTH),
        ):
            self._call(ip=_IP, source_ip=_SOURCE_IP)
        mock_resolve.assert_called_with(_IP, source_ip=_SOURCE_IP)

    def test_resolve_mac_called_with_non_empty_source_ip(self):
        """When caller provides a bound source_ip, it is forwarded to resolve_mac."""
        src = "192.168.1.5"
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=_MAC) as mock_resolve,
            patch(_PATCH_SYNTH),
        ):
            self._call(ip=_IP, source_ip=src)
        mock_resolve.assert_called_with(_IP, source_ip=src)

    def test_synthesize_mac_id_called_with_ip(self):
        """synthesize_mac_id receives the probed IP as its sole positional arg."""
        with (
            patch(
                "tuner_app.scanner.discover.miner_http_request",
                side_effect=self._epic_side_effect,
            ),
            patch(_PATCH_RESOLVE, return_value=None),
            patch(_PATCH_SYNTH, return_value=_SYN_MAC) as mock_synth,
        ):
            self._call(ip=_IP)
        mock_synth.assert_called_with(_IP)
