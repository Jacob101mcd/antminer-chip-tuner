"""Tests for BraiinsMinerAPI HTTP/JSON REST client.

Wire-format tests follow the same pattern as test_bixbit_miner_api.py.

Two transport paths exist in BraiinsMinerAPI:
  - Unauthenticated:  _raw_request → miner_http_request (stdlib shim)
  - Authenticated:    _raw_request_with_token → http.client.HTTPConnection (direct)

We mock at the appropriate boundary for each path:
  - tuner_app.miner.braiins.miner_http_request  — for login (unauthenticated)
  - braiins._raw_request_with_token             — for post-login requests
  - braiins._authed_request                     — for high-level method tests
    (avoids http.client plumbing when we just want to assert the body sent)
"""

import json
import unittest
from unittest.mock import patch

from tuner_app.miner.braiins import BraiinsMinerAPI
from tuner_app.miner.exceptions import MinerCommandError, MinerOfflineError
from tuner_app.miner.types import HardwareTopology, MinerSummary

PATCH_HTTP = "tuner_app.miner.braiins.miner_http_request"

# ---------------------------------------------------------------------------
# Canonical response bodies
# ---------------------------------------------------------------------------

_LOGIN_BODY = json.dumps({"token": "tok-abc", "timeout_s": 3600}).encode()
_LOGIN_OK = (200, [], _LOGIN_BODY)

_DETAILS_RAW = {
    "uid": "uid1",
    "platform": "am3",
    "bos_mode": "plus",
    "hostname": "miner01",
    "mac_address": "aa:bb:cc:dd:ee:ff",
    "system_uptime": "1d",
    "bosminer_uptime_s": 3600,
    "system_uptime_s": 7200,
    "status": 2,
    "kernel_version": "5.4",
    "control_board_soc_family": "zynq",
    "miner_identity": {"brand": 1, "miner_model": "Antminer S19", "name": "S19"},
}
_STATS_RAW = {
    "miner_stats": {
        "real_hashrate": {
            "last_1m": {"gigahash_per_second": 100_000.0},
        },
        "found_blocks": 0,
    },
    "power_stats": {
        "approximated_consumption": {"watt": 3500},
    },
}
_COOLING_RAW = {
    "fans": [{"rpm": 3600, "position": 0, "target_speed_ratio": 0.7}],
}


def _make_miner() -> BraiinsMinerAPI:
    return BraiinsMinerAPI("1.2.3.4", port=80, password="letmein")


# ===========================================================================
# TestBraiinsAuth
# ===========================================================================


class TestBraiinsAuth(unittest.TestCase):
    """authenticate() login round-trips and token caching."""

    @patch(PATCH_HTTP)
    def test_authenticate_happy_path(self, mock_req):
        """Login returns 200 + token → authenticate() True, token cached."""
        mock_req.return_value = _LOGIN_OK
        miner = _make_miner()
        result = miner.authenticate()
        self.assertTrue(result)
        self.assertEqual(miner._token, "tok-abc")
        # Verify the login call went to the right path.
        args = mock_req.call_args
        self.assertIn("/api/v1/auth/login", args[0])

    @patch(PATCH_HTTP)
    def test_authenticate_invalid_credentials(self, mock_req):
        """Login returns 401 → authenticate() False, no token cached."""
        mock_req.return_value = (401, [], b"Unauthorized")
        miner = _make_miner()
        result = miner.authenticate()
        self.assertFalse(result)
        self.assertIsNone(miner._token)

    @patch(PATCH_HTTP)
    def test_authenticate_offline(self, mock_req):
        """Connection error during login → authenticate() False."""
        mock_req.side_effect = OSError("connection refused")
        miner = _make_miner()
        result = miner.authenticate()
        self.assertFalse(result)
        self.assertIsNone(miner._token)

    def test_token_refresh_on_401(self):
        """First authed request returns 401 → re-login → retry succeeds."""
        miner = _make_miner()
        miner._token = "old-token"
        miner._token_expires_at = None  # never-expire sentinel

        ensure_call_count = [0]
        authed_call_count = [0]

        def _fake_raw_request_with_token(path, method="GET", body=None):
            authed_call_count[0] += 1
            if authed_call_count[0] == 1:
                return (401, b"")
            return (200, json.dumps({"fans": []}).encode())

        def _fake_ensure_token():
            ensure_call_count[0] += 1
            miner._token = "new-token"

        with (
            patch.object(
                miner,
                "_raw_request_with_token",
                side_effect=_fake_raw_request_with_token,
            ),
            patch.object(miner, "_ensure_token", side_effect=_fake_ensure_token),
        ):
            status, body = miner._authed_request("/api/v1/cooling/state")

        self.assertEqual(status, 200)
        # _ensure_token called twice: once at entry, once after 401 refresh.
        self.assertEqual(ensure_call_count[0], 2)
        # _raw_request_with_token called twice: once returning 401, once 200.
        self.assertEqual(authed_call_count[0], 2)

    def test_token_refresh_only_one_retry(self):
        """Retry after refresh also returns 401 → MinerCommandError (no loop)."""
        miner = _make_miner()
        miner._token = "old-token"
        miner._token_expires_at = None

        def _always_401(path, method="GET", body=None):
            return (401, b"")

        def _fake_ensure_token():
            miner._token = "refreshed-token"

        with (
            patch.object(miner, "_raw_request_with_token", side_effect=_always_401),
            patch.object(miner, "_ensure_token", side_effect=_fake_ensure_token),
            self.assertRaises(MinerCommandError),
        ):
            miner._authed_request("/api/v1/miner/details")


# ===========================================================================
# TestBraiinsSummary
# ===========================================================================


class TestBraiinsSummary(unittest.TestCase):
    """summary() synthesizes MinerSummary from three GET calls."""

    def test_summary_happy_path(self):
        """Three authed GETs all succeed → MinerSummary with correct fields."""
        miner = _make_miner()
        with (
            patch.object(miner, "_summary_details_raw", return_value=_DETAILS_RAW),
            patch.object(miner, "_summary_stats_raw", return_value=_STATS_RAW),
            patch.object(miner, "_summary_cooling_raw", return_value=_COOLING_RAW),
        ):
            result = miner.summary()

        self.assertIsInstance(result, MinerSummary)
        # Status 2 → "normal"
        self.assertEqual(result.operating_state, "normal")
        # 100_000 GH/s / 1000 = 100 TH/s
        self.assertAlmostEqual(result.hashrate_ths, 100.0)
        self.assertEqual(result.power_w, 3500.0)
        self.assertEqual(result.fan_speed, 3600)
        self.assertEqual(result.hostname, "miner01")
        self.assertEqual(result.model, "Antminer S19")
        self.assertEqual(result.boards, [])
        # is_hashing: hashrate_ths > 0 → True
        self.assertTrue(result.is_hashing)
        # Voltage always None for Braiins
        self.assertIsNone(result.target_voltage_mv)
        self.assertIsNone(result.output_voltage_mv)

    def test_summary_partial_offline(self):
        """details succeeds but stats raises MinerOfflineError → propagates."""
        miner = _make_miner()
        with (
            patch.object(miner, "_summary_details_raw", return_value=_DETAILS_RAW),
            patch.object(
                miner,
                "_summary_stats_raw",
                side_effect=MinerOfflineError("offline"),
            ),
            self.assertRaises(MinerOfflineError),
        ):
            miner.summary()


# ===========================================================================
# TestBraiinsControlEndpoints
# ===========================================================================


class TestBraiinsControlEndpoints(unittest.TestCase):
    """set_power_limit, start_mining, stop_mining, reboot — correct HTTP shape."""

    def _miner_with_token(self) -> BraiinsMinerAPI:
        """Return a miner with a pre-seeded token so login is skipped."""
        miner = _make_miner()
        miner._token = "test-token"
        miner._token_expires_at = None  # never expires
        return miner

    def test_set_power_limit_happy_path(self):
        """set_power_limit(3500) → PUT /api/v1/performance/power-target {"watt":3500}."""
        miner = self._miner_with_token()
        sent_body: dict = {}

        def _fake_authed(path, method="GET", body=None):
            sent_body["path"] = path
            sent_body["method"] = method
            sent_body["body"] = body
            return (200, json.dumps({"watt": 3500}).encode())

        with patch.object(miner, "_authed_request", side_effect=_fake_authed):
            result = miner.set_power_limit(3500)

        self.assertEqual(sent_body["path"], "/api/v1/performance/power-target")
        self.assertEqual(sent_body["method"], "PUT")
        self.assertEqual(sent_body["body"], {"watt": 3500})
        self.assertEqual(result, {"watt": 3500})

    def test_set_power_limit_truncates_to_int(self):
        """set_power_limit(3500.9) sends {"watt": 3500} (int cast)."""
        miner = self._miner_with_token()
        sent_body: dict = {}

        def _fake_authed(path, method="GET", body=None):
            sent_body["body"] = body
            return (200, json.dumps({"watt": 3500}).encode())

        with patch.object(miner, "_authed_request", side_effect=_fake_authed):
            miner.set_power_limit(3500.9)

        self.assertEqual(sent_body["body"]["watt"], 3500)
        self.assertIsInstance(sent_body["body"]["watt"], int)

    def test_start_mining_happy_path(self):
        """start_mining() sends PUT /api/v1/actions/start."""
        miner = self._miner_with_token()
        sent_calls: list = []

        def _fake_authed(path, method="GET", body=None):
            sent_calls.append((path, method))
            return (200, b"false")

        with patch.object(miner, "_authed_request", side_effect=_fake_authed):
            result = miner.start_mining()

        self.assertEqual(sent_calls[0], ("/api/v1/actions/start", "PUT"))
        self.assertFalse(result)  # json.loads(b"false") == False

    def test_stop_mining_happy_path(self):
        """stop_mining() sends PUT /api/v1/actions/stop."""
        miner = self._miner_with_token()
        sent_calls: list = []

        def _fake_authed(path, method="GET", body=None):
            sent_calls.append((path, method))
            return (200, b"true")

        with patch.object(miner, "_authed_request", side_effect=_fake_authed):
            result = miner.stop_mining()

        self.assertEqual(sent_calls[0], ("/api/v1/actions/stop", "PUT"))
        self.assertTrue(result)

    def test_reboot_happy_path(self):
        """reboot() sends PUT /api/v1/actions/reboot; 204 is accepted."""
        miner = self._miner_with_token()
        sent_calls: list = []

        def _fake_authed(path, method="GET", body=None):
            sent_calls.append((path, method))
            return (204, b"")  # 204 No Content

        with patch.object(miner, "_authed_request", side_effect=_fake_authed):
            miner.reboot()  # must not raise

        self.assertEqual(sent_calls[0], ("/api/v1/actions/reboot", "PUT"))

    def test_reboot_non_2xx_raises(self):
        """reboot() raises MinerCommandError on non-2xx response."""
        miner = self._miner_with_token()
        with (
            patch.object(miner, "_authed_request", return_value=(500, b"Server Error")),
            self.assertRaises(MinerCommandError),
        ):
            miner.reboot()


# ===========================================================================
# TestBraiinsNotImplemented
# ===========================================================================


class TestBraiinsNotImplemented(unittest.TestCase):
    """Methods not supported by Braiins OS raise NotImplementedError."""

    def setUp(self):
        self.miner = _make_miner()

    def test_set_voltage_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.miner.set_voltage(14000)

    def test_set_clock_all_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.miner.set_clock_all(490)

    def test_set_clock_board_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.miner.set_clock_board([])

    def test_set_clock_chip_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.miner.set_clock_chip(0, [])

    def test_set_coin_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.miner.set_coin("BTC", [])


# ===========================================================================
# TestBraiinsTypedQueries
# ===========================================================================


class TestBraiinsTypedQueries(unittest.TestCase):
    """Typed query methods that return empty lists for Braiins."""

    def setUp(self):
        self.miner = _make_miner()

    def test_clocks_returns_empty_list(self):
        self.assertEqual(self.miner.clocks(), [])

    def test_temps_returns_empty_list(self):
        self.assertEqual(self.miner.temps(), [])

    def test_temps_chip_returns_empty_list(self):
        self.assertEqual(self.miner.temps_chip(), [])

    def test_hashrate_returns_empty_list(self):
        self.assertEqual(self.miner.hashrate(), [])


# ===========================================================================
# TestBraiinsFirmwareType
# ===========================================================================


class TestBraiinsFirmwareType(unittest.TestCase):
    def test_firmware_type_returns_braiins(self):
        miner = _make_miner()
        self.assertEqual(miner.firmware_type(), "braiins")


# ===========================================================================
# TestBraiinsEnsureToken
# ===========================================================================


class TestBraiinsEnsureToken(unittest.TestCase):
    """_ensure_token caches the token and reuses it on the next call."""

    @patch(PATCH_HTTP)
    def test_ensure_token_called_only_once_when_token_valid(self, mock_req):
        """_ensure_token makes one login call; subsequent calls hit the cache."""
        mock_req.return_value = _LOGIN_OK
        miner = _make_miner()
        miner._ensure_token()
        miner._ensure_token()  # should NOT call miner_http_request again
        self.assertEqual(mock_req.call_count, 1)
        self.assertEqual(miner._token, "tok-abc")

    @patch(PATCH_HTTP)
    def test_ensure_token_missing_token_field_raises(self, mock_req):
        """Login response with missing 'token' field raises MinerCommandError."""
        mock_req.return_value = (200, [], json.dumps({"timeout_s": 3600}).encode())
        miner = _make_miner()
        with self.assertRaises(MinerCommandError):
            miner._ensure_token()

    @patch(PATCH_HTTP)
    def test_ensure_token_invalid_json_raises(self, mock_req):
        """Login returns non-JSON → MinerCommandError."""
        mock_req.return_value = (200, [], b"not-json")
        miner = _make_miner()
        with self.assertRaises(MinerCommandError):
            miner._ensure_token()


# ===========================================================================
# TestBraiinsTuningStrategy
# ===========================================================================


class TestBraiinsTuningStrategy(unittest.TestCase):
    def test_tuning_strategy_returns_wattage_search(self):
        miner = _make_miner()
        self.assertEqual(miner.tuning_strategy(), "wattage_search")


# ===========================================================================
# TestBraiinsHardwareTopologyInApiTest
# ===========================================================================


class TestBraiinsHardwareTopologyInApiTest(unittest.TestCase):
    def test_hardware_topology_reads_hashboards_constraints(self):
        miner = _make_miner()
        mock_constraints = {"hashboards_constraints": {"count": 3}}
        with patch.object(miner, "_get_json", return_value=mock_constraints):
            result = miner.hardware_topology()
        self.assertIsInstance(result, HardwareTopology)
        self.assertEqual(result.num_boards, 3)
        self.assertEqual(result.chips_per_board, 0)
        self.assertEqual(result.psu_min_mv, 11877)
        self.assertEqual(result.psu_max_mv, 15182)
        self.assertFalse(result.psu_bounds_verified)
        self.assertEqual(result.psu_bounds_source, "not-applicable:firmware-owned-vf")

    def test_hardware_topology_falls_back_on_missing_constraints(self):
        miner = _make_miner()
        with patch.object(miner, "_get_json", return_value={}):
            result = miner.hardware_topology()
        self.assertEqual(result.num_boards, 3)
        self.assertEqual(result.chips_per_board, 0)
        self.assertEqual(result.psu_min_mv, 11877)
        self.assertEqual(result.psu_max_mv, 15182)
