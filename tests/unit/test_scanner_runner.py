import threading
import time
from unittest import TestCase
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.scanner.discover import ProbeResult
from tuner_app.scanner.runner import Scanner

_SCAN_KEYS = (
    "SCAN_IP_RANGES",
    "MINER_IPS",
    "SOURCE_IP",
    "SCAN_PASSWORDS",
    "SCAN_TIMEOUT_SEC",
    "SCAN_CONCURRENCY",
    "SCAN_AUTO_REGISTER",
    "API_PORT",
    "SCAN_INTERVAL_MIN",
)

_DEFAULTS_SCAN = {
    "SCAN_IP_RANGES": ["10.0.0.0/30"],
    "MINER_IPS": [],
    "SOURCE_IP": "",
    "SCAN_PASSWORDS": ["letmein"],
    "SCAN_TIMEOUT_SEC": 5,
    "SCAN_CONCURRENCY": 10,
    "SCAN_AUTO_REGISTER": False,
    "API_PORT": 4028,
    "SCAN_INTERVAL_MIN": 10,
}


def _setup_fleet_ops(overrides=None):
    """Set fleet_ops keys; return snapshot of old values for teardown."""
    fo = state.CONFIG["fleet_ops"]
    snapshot = {k: fo[k] for k in _SCAN_KEYS if k in fo}
    cfg = {**_DEFAULTS_SCAN, **(overrides or {})}
    for k, v in cfg.items():
        fo[k] = v
    return snapshot


def _teardown_fleet_ops(snapshot):
    fo = state.CONFIG["fleet_ops"]
    # Restore snapshot keys
    for k, v in snapshot.items():
        fo[k] = v
    # Remove keys that weren't originally present
    for k in _SCAN_KEYS:
        if k not in snapshot:
            fo.pop(k, None)


class TestManualOnlyScannerMode(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops({"SCAN_INTERVAL_MIN": 0})
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        self.scanner.stop()
        _teardown_fleet_ops(self._snapshot)

    def test_zero_interval_waits_for_explicit_request(self):
        with patch.object(self.scanner, "_scan_cycle") as scan:
            self.scanner.start()
            time.sleep(0.05)
            scan.assert_not_called()
            self.scanner.request_scan_now()
            deadline = time.monotonic() + 1
            while scan.call_count == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            scan.assert_called_once()


class TestResolveSourceIpCalledOncePerScan(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops(
            {"SCAN_IP_RANGES": ["10.0.0.0/29"], "MINER_IPS": ["1.2.3.4"]}
        )
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.MINER_CONFIGS = {}

    def test_resolve_source_ip_called_once_per_scan(self):
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip") as mock_resolve,
        ):
            mock_probe.side_effect = lambda ip, *args, **kwargs: ProbeResult(
                ip=ip,
                reachable=False,
                vendor_match=False,
                password_found=None,
                hostname=None,
                error="No vendor match",
                firmware_type=None,
            )
            mock_resolve.return_value = "192.168.1.100"

            self.scanner._scan_cycle()

            mock_resolve.assert_called_once_with("1.2.3.4", 4028)
            self.assertEqual(mock_probe.call_count, 8)


class TestResolveSourceIpSkippedWhenConfigSet(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops({"SOURCE_IP": "192.168.1.5"})
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.MINER_CONFIGS = {}

    def test_resolve_source_ip_skipped_when_config_source_ip_set(self):
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip") as mock_resolve,
        ):
            mock_probe.return_value = ProbeResult(
                ip="1.2.3.4",
                reachable=False,
                vendor_match=False,
                password_found=None,
                hostname=None,
                error="No vendor match",
                firmware_type=None,
            )
            mock_resolve.return_value = "192.168.1.100"

            self.scanner._scan_cycle()

            mock_resolve.assert_not_called()
            for call_args in mock_probe.call_args_list:
                self.assertEqual(call_args[1]["source_ip"], "192.168.1.5")


class TestResolveSourceIpFallbackNoMiners(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops()
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.MINER_CONFIGS = {}

    def test_resolve_source_ip_falls_back_when_no_known_miners(self):
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip") as mock_resolve,
        ):
            mock_probe.return_value = ProbeResult(
                ip="1.2.3.4",
                reachable=False,
                vendor_match=False,
                password_found=None,
                hostname=None,
                error="No vendor match",
                firmware_type=None,
            )
            mock_resolve.return_value = "192.168.1.100"

            self.scanner._scan_cycle()

            mock_resolve.assert_not_called()
            for call_args in mock_probe.call_args_list:
                self.assertEqual(call_args[1]["source_ip"], "")


class TestStatusDiscoveredIncrementallyUpdated(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops()
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.MINER_CONFIGS = {}

    def test_status_discovered_updated_incrementally(self):
        # /30 gives IPs: 10.0.0.0, 10.0.0.1, 10.0.0.2, 10.0.0.3
        # Use per-call events to gate each probe; first call returns a found miner.
        gate_events = [threading.Event() for _ in range(4)]
        call_order: list = []
        call_lock = threading.Lock()

        def side_effect(ip, *args, **kwargs):
            with call_lock:
                idx = len(call_order)
                call_order.append(ip)
            gate_events[idx].wait(timeout=5)
            if idx == 0:
                return ProbeResult(
                    ip=ip,
                    reachable=True,
                    vendor_match=True,
                    password_found="letmein",
                    hostname="miner1",
                    error=None,
                    firmware_type="epic",
                )
            return ProbeResult(
                ip=ip,
                reachable=False,
                vendor_match=False,
                password_found=None,
                hostname=None,
                error="No vendor match",
                firmware_type=None,
            )

        with (
            patch("tuner_app.scanner.runner.probe_miner", side_effect=side_effect),
            patch("tuner_app.scanner.runner.resolve_source_ip", return_value=""),
        ):
            thread = threading.Thread(target=self.scanner._scan_cycle)
            thread.start()

            # Release first probe and wait for discovered to populate
            gate_events[0].set()
            deadline = 3.0
            increment = 0.05
            elapsed = 0.0
            while elapsed < deadline:
                if len(self.scanner.get_status()["discovered"]) >= 1:
                    break
                threading.Event().wait(increment)
                elapsed += increment

            self.assertGreaterEqual(
                len(self.scanner.get_status()["discovered"]),
                1,
                "discovered must be updated before scan cycle completes",
            )

            # Release remaining probes
            for i in range(1, 4):
                gate_events[i].set()
            thread.join(timeout=5)

            status = self.scanner.get_status()
            self.assertEqual(status["progress"], 4)


class TestTotalFieldPopulated(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops()
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.MINER_CONFIGS = {}

    def test_total_field_populated(self):
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip") as mock_resolve,
        ):
            mock_probe.return_value = ProbeResult(
                ip="1.2.3.4",
                reachable=False,
                vendor_match=False,
                password_found=None,
                hostname=None,
                error="No vendor match",
                firmware_type=None,
            )
            mock_resolve.return_value = ""

            self.scanner._scan_cycle()

            status = self.scanner.get_status()
            self.assertEqual(status["total"], 4)


class TestRegisterFailureDoesNotCrashScanner(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops(
            {"SCAN_IP_RANGES": ["10.0.0.0/31"], "SCAN_AUTO_REGISTER": True}
        )
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.MINER_CONFIGS = {}

    def test_register_failure_does_not_crash_scanner(self):
        with (
            patch("tuner_app.scanner.runner.probe_miner") as mock_probe,
            patch("tuner_app.scanner.runner.resolve_source_ip") as mock_resolve,
            patch("tuner_app.scanner.runner.save_config_to_disk") as mock_save,
            patch("tuner_app.scanner.runner._register_miner_locked") as mock_register,
        ):
            mock_probe.return_value = ProbeResult(
                ip="10.0.0.1",
                reachable=True,
                vendor_match=True,
                password_found="letmein",
                hostname="miner1",
                error=None,
                firmware_type="epic",
            )
            mock_resolve.return_value = ""
            mock_save.return_value = None
            mock_register.side_effect = [RuntimeError("simulated"), None]

            self.scanner._scan_cycle()

            status = self.scanner.get_status()
            self.assertEqual(len(status["discovered"]), 2)
            self.assertIn("register failed", str(status["errors"]))
            self.assertEqual(status["state"], "idle")


class TestScanCycleExceptionCaughtInRun(TestCase):
    def setUp(self):
        self._snapshot = _setup_fleet_ops({"SCAN_IP_RANGES": ["10.0.0.0/31"]})
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.MINER_CONFIGS = {}

    def test_scan_cycle_exception_caught_in_run(self):
        done_event = threading.Event()

        def side_effect():
            if not done_event.is_set():
                done_event.set()
                raise RuntimeError("boom")
            return None

        with patch.object(self.scanner, "_scan_cycle", side_effect=side_effect):
            thread = threading.Thread(target=self.scanner._run)
            thread.start()

            # Wait for the exception to be raised and status updated.
            # done_event fires just before RuntimeError is raised; we then poll
            # for the error to appear in the status dict (avoids fixed sleeps).
            done_event.wait(timeout=3)
            deadline = 2.0
            elapsed = 0.0
            interval = 0.05
            while elapsed < deadline:
                status = self.scanner.get_status()
                if status["errors"]:
                    break
                threading.Event().wait(interval)
                elapsed += interval

            self.assertIn("scan cycle error: boom", str(status["errors"]))

            # Stop the thread
            self.scanner._stop_event.set()
            self.scanner._wake_event.set()
            thread.join(timeout=2)


class TestScanIpBlacklist(TestCase):
    """SCAN_IP_BLACKLIST excludes IPs from probing even when they fall in a scan range."""

    def setUp(self):
        self._snapshot = _setup_fleet_ops({"SOURCE_IP": "192.168.1.5"})
        state.CONFIG["fleet_ops"]["SCAN_IP_BLACKLIST"] = ["10.0.0.2"]
        state.MINER_CONFIGS = {}
        self.scanner = Scanner(manager=MagicMock())

    def tearDown(self):
        _teardown_fleet_ops(self._snapshot)
        state.CONFIG["fleet_ops"].pop("SCAN_IP_BLACKLIST", None)
        state.MINER_CONFIGS = {}

    def test_blacklisted_ip_not_probed(self):
        with patch("tuner_app.scanner.runner.probe_miner") as mock_probe:
            mock_probe.side_effect = lambda ip, *a, **kw: ProbeResult(
                ip=ip,
                reachable=False,
                vendor_match=False,
                password_found=None,
                hostname=None,
                error="No vendor match",
                firmware_type=None,
            )
            self.scanner._scan_cycle()

            probed = {call_args[0][0] for call_args in mock_probe.call_args_list}
            self.assertEqual(probed, {"10.0.0.0", "10.0.0.1", "10.0.0.3"})
            self.assertNotIn("10.0.0.2", probed)
            self.assertEqual(mock_probe.call_count, 3)

    def test_blacklist_range_excludes_multiple_ips(self):
        state.CONFIG["fleet_ops"]["SCAN_IP_BLACKLIST"] = ["10.0.0.0/31"]  # 10.0.0.0 + 10.0.0.1
        with patch("tuner_app.scanner.runner.probe_miner") as mock_probe:
            mock_probe.side_effect = lambda ip, *a, **kw: ProbeResult(
                ip=ip,
                reachable=False,
                vendor_match=False,
                password_found=None,
                hostname=None,
                error="No vendor match",
                firmware_type=None,
            )
            self.scanner._scan_cycle()

            probed = {call_args[0][0] for call_args in mock_probe.call_args_list}
            self.assertEqual(probed, {"10.0.0.2", "10.0.0.3"})

    def test_malformed_blacklist_aborts_scan_with_error(self):
        state.CONFIG["fleet_ops"]["SCAN_IP_BLACKLIST"] = ["not-an-ip"]
        with patch("tuner_app.scanner.runner.probe_miner") as mock_probe:
            self.scanner._scan_cycle()
            mock_probe.assert_not_called()
            status = self.scanner.get_status()
            self.assertEqual(status["state"], "idle")
            self.assertTrue(
                any("SCAN_IP_BLACKLIST" in e for e in status["errors"]),
                f"expected SCAN_IP_BLACKLIST error in status, got {status['errors']!r}",
            )
