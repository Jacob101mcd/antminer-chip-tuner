"""A11: Scanner detects IP changes for known MACs.

Integration-style tests that drive a full Scanner._scan_cycle and assert the
DHCP-move invariant: when a probe finds a known MAC at a different IP, the
scanner calls manager.refresh_engine_ip(mac, new_ip) and updates
MINER_CONFIGS[mac]["ip"] WITHOUT engine teardown.

Companion to test_scanner_register.py's TestScannerRegisterLocked which
covers the same wiring at the _register_locked entry point with a single
direct call.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.scanner.discover import ProbeResult
from tuner_app.scanner.runner import Scanner

_MAC_A = "aa:bb:cc:dd:ee:01"
_OLD_IP = "192.0.2.50"
_NEW_IP = "192.0.2.99"


def _setup_scan_config(ranges, **extras):
    """Snapshot fleet_ops, set scan-relevant keys; returns the snapshot for teardown."""
    fo = state.CONFIG["fleet_ops"]
    snapshot = {
        k: fo.get(k)
        for k in (
            "SCAN_IP_RANGES",
            "SCAN_IP_BLACKLIST",
            "SCAN_PASSWORDS",
            "SCAN_TIMEOUT_SEC",
            "SCAN_CONCURRENCY",
            "SCAN_INTERVAL_MIN",
            "SCAN_AUTO_REGISTER",
            "API_PORT",
            "SOURCE_IP",
            "MINER_IPS",
        )
    }
    fo["SCAN_IP_RANGES"] = ranges
    fo["SCAN_IP_BLACKLIST"] = []
    fo["SCAN_PASSWORDS"] = ["letmein"]
    fo["SCAN_TIMEOUT_SEC"] = 1.0
    fo["SCAN_CONCURRENCY"] = 4
    fo["SCAN_AUTO_REGISTER"] = True
    fo["API_PORT"] = 4028
    fo["SOURCE_IP"] = ""
    fo["MINER_IPS"] = list(extras.get("MINER_IPS", []))
    return snapshot


def _restore_scan_config(snapshot):
    fo = state.CONFIG["fleet_ops"]
    for k, v in snapshot.items():
        if v is None:
            fo.pop(k, None)
        else:
            fo[k] = v


class TestScannerIpChangeForKnownMac(unittest.TestCase):
    """The full _scan_cycle wires probe → _register_locked → refresh_engine_ip.

    Pre-conditions: MINER_CONFIGS already has a v4 entry for _MAC_A at _OLD_IP
    (a prior scan registered it). Note that we set MINER_IPS to NOT include
    the new IP so it's eligible for scanning (the cycle skips already-known
    IPs); the scanner detects the MAC mid-probe and routes through the
    refresh path.
    """

    def setUp(self):
        # Ensure scanned IP is not in the "already registered" filter list.
        # MINER_CONFIGS still has the old entry under MAC.
        self._snapshot = _setup_scan_config(
            ranges=[_NEW_IP],  # /32 single-IP range so we probe exactly one IP
            MINER_IPS=[_OLD_IP],  # old IP — new_IP is "unknown", so probe runs
        )
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS[_MAC_A] = {
            "ip": _OLD_IP,
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
        }

    def tearDown(self):
        _restore_scan_config(self._snapshot)
        state.MINER_CONFIGS.clear()

    def test_scan_cycle_known_mac_at_new_ip_calls_refresh_engine_ip(self):
        """End-to-end: probe returns MAC=A at IP=NEW; scanner registers under
        the same MAC and calls manager.refresh_engine_ip(MAC, NEW_IP)."""
        manager = MagicMock()
        scanner = Scanner(manager)

        probe_result = ProbeResult(
            ip=_NEW_IP,
            reachable=True,
            vendor_match=True,
            password_found="letmein",
            hostname="miner-example",
            error=None,
            firmware_type="epic",
            mac=_MAC_A,
            id_synthesized=False,
        )
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip", return_value=""),
            patch("tuner_app.scanner.runner.save_config_to_disk"),
        ):
            mock_probe.return_value = probe_result
            scanner._scan_cycle()

        # The refresh path fired with the canonical (mac, new_ip)
        manager.refresh_engine_ip.assert_called_once_with(_MAC_A, _NEW_IP)
        # MINER_CONFIGS retained the same MAC key, ip field updated to new IP
        self.assertEqual(state.MINER_CONFIGS[_MAC_A]["ip"], _NEW_IP)
        # No second MAC entry created
        self.assertEqual(len(state.MINER_CONFIGS), 1)

    def test_scan_cycle_no_engine_teardown_on_ip_change(self):
        """The existing engine instance must NOT be destroyed when a known
        MAC is rediscovered at a new IP. The tuning thread should keep
        running uninterrupted."""
        manager = MagicMock()
        scanner = Scanner(manager)
        probe_result = ProbeResult(
            ip=_NEW_IP,
            reachable=True,
            vendor_match=True,
            password_found="letmein",
            hostname=None,
            error=None,
            firmware_type="epic",
            mac=_MAC_A,
            id_synthesized=False,
        )
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip", return_value=""),
            patch("tuner_app.scanner.runner.save_config_to_disk"),
        ):
            mock_probe.return_value = probe_result
            scanner._scan_cycle()

        # The pop_engine teardown path is NOT used for IP-change
        manager.pop_engine.assert_not_called()


class TestScannerNewMacBypassesRefresh(unittest.TestCase):
    """When the discovered MAC is new (no MINER_CONFIGS entry yet),
    refresh_engine_ip is NOT called — the standard get_engine spawn path runs.
    """

    def setUp(self):
        self._snapshot = _setup_scan_config(
            ranges=[_NEW_IP],
            MINER_IPS=[],
        )
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        _restore_scan_config(self._snapshot)
        state.MINER_CONFIGS.clear()

    def test_new_mac_no_refresh_engine_ip_call(self):
        manager = MagicMock()
        scanner = Scanner(manager)
        probe_result = ProbeResult(
            ip=_NEW_IP,
            reachable=True,
            vendor_match=True,
            password_found="letmein",
            hostname=None,
            error=None,
            firmware_type="epic",
            mac=_MAC_A,
            id_synthesized=False,
        )
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip", return_value=""),
            patch("tuner_app.scanner.runner.save_config_to_disk"),
        ):
            mock_probe.return_value = probe_result
            scanner._scan_cycle()
        manager.refresh_engine_ip.assert_not_called()
        manager.get_engine.assert_called_once_with(_MAC_A)


if __name__ == "__main__":
    unittest.main(verbosity=2)
