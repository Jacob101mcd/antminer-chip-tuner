"""Integration tests for the config-write route handlers.

Covers:
- POST /tuner/config/defaults (new per-platform shape; legacy flat shape now rejected)
- POST /tuner/config/fleet_ops (new endpoint)
- POST /tuner/config/miner/{ip} (platform-aware path, unchanged shape)
"""

from __future__ import annotations

import json
import threading
import unittest
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.config.defaults import apply_defaults
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server


class _StubManager:
    """Minimal stub — config-write tests don't exercise the manager."""

    def __init__(self):
        self.engines = {}


class TestConfigDefaultsNewShape(unittest.TestCase):
    """Tests for POST /tuner/config/defaults with the new per-platform shape."""

    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.MINER_CONFIGS.clear()
        # Re-apply defaults so CONFIG is in a known clean state for each test.
        apply_defaults()

        import tuner_app.config.persistence as _p

        self._orig_save = _p.save_config_to_disk
        _p.save_config_to_disk = lambda: None

        from tuner_app.http_server.handlers import miners_routes as _mr

        self._orig_mr_save = _mr.save_config_to_disk
        _mr.save_config_to_disk = lambda: None

        self._stub = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"
        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        import tuner_app.config.persistence as _p

        _p.save_config_to_disk = self._orig_save

        from tuner_app.http_server.handlers import miners_routes as _mr

        _mr.save_config_to_disk = self._orig_mr_save

        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.MINER_CONFIGS.clear()

    def _post(self, path, body, cookie=None):
        req = request.Request(self.base + path, method="POST")
        req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        data = json.dumps(body).encode()
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except error.HTTPError as ex:
            return ex.code, json.loads(ex.read().decode())

    # ── NEW SHAPE ──────────────────────────────────────────────────────────────

    def test_new_shape_writes_to_specific_platform_only(self):
        """BOARD_MAX_TEMP update for "bixbit" should NOT change the epic bucket."""
        epic_before = state.CONFIG["defaults"]["epic"].get("BOARD_MAX_TEMP")
        # Use 80 — within the (50, 85) bounds.
        status, data = self._post(
            "/tuner/config/defaults",
            {"platform": "bixbit", "defaults": {"BOARD_MAX_TEMP": 80}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        self.assertEqual(state.CONFIG["defaults"]["bixbit"]["BOARD_MAX_TEMP"], 80)
        # epic bucket must be untouched
        self.assertEqual(state.CONFIG["defaults"]["epic"].get("BOARD_MAX_TEMP"), epic_before)

    def test_new_shape_writes_to_all_supported_platforms(self):
        """Each platform bucket is writable individually."""
        for platform in ("epic", "bixbit", "luxos", "braiins"):
            status, data = self._post(
                "/tuner/config/defaults",
                {"platform": platform, "defaults": {"BOARD_MAX_TEMP": 80}},
                cookie=self.cookie,
            )
            self.assertEqual(status, 200, f"Failed for platform={platform}: {data}")
            self.assertTrue(data["updated"], f"updated=False for platform={platform}: {data}")
            self.assertEqual(state.CONFIG["defaults"][platform]["BOARD_MAX_TEMP"], 80)

    def test_new_shape_rejects_unknown_platform(self):
        status, data = self._post(
            "/tuner/config/defaults",
            {"platform": "unknown_fw", "defaults": {"BOARD_MAX_TEMP": 75}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("platform must be one of" in e for e in data["errors"]))

    def test_new_shape_rejects_fleet_ops_keys(self):
        """Fleet-ops key SCAN_TIMEOUT_SEC must be rejected from /defaults new shape."""
        status, data = self._post(
            "/tuner/config/defaults",
            {"platform": "epic", "defaults": {"SCAN_TIMEOUT_SEC": 5}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("fleet-ops key" in e for e in data["errors"]))

    def test_new_shape_rejects_extra_top_level_keys(self):
        """Body with extra keys beside 'platform' and 'defaults' is 400."""
        status, data = self._post(
            "/tuner/config/defaults",
            {"platform": "epic", "defaults": {"BOARD_MAX_TEMP": 80}, "extra": "bad"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("unexpected top-level keys" in e for e in data["errors"]))

    def test_new_shape_rejects_missing_defaults_key(self):
        """If 'platform' present but 'defaults' is not a dict, reject."""
        status, data = self._post(
            "/tuner/config/defaults",
            {"platform": "epic", "defaults": "not_a_dict"},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])

    def test_new_shape_rejects_invalid_value(self):
        """Validation still runs; out-of-bounds value should be rejected.
        The handler returns HTTP 200 with updated=False + errors (consistent
        with the existing /defaults behavior — not a 4xx status code)."""
        status, data = self._post(
            "/tuner/config/defaults",
            {"platform": "epic", "defaults": {"BOARD_MAX_TEMP": 9999}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertFalse(data["updated"])
        self.assertGreater(len(data["errors"]), 0)

    # ── LEGACY SHAPE NOW REJECTED ─────────────────────────────────────────────

    def test_legacy_shape_flat_body_now_rejected(self):
        """Flat body without 'platform' is rejected with HTTP 400 (legacy path removed)."""
        status, data = self._post(
            "/tuner/config/defaults",
            {"BOARD_MAX_TEMP": 80},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("'platform' is required" in e for e in data["errors"]))

    def test_legacy_shape_fleet_ops_keys_without_platform_now_rejected(self):
        """Flat body with SCAN_TIMEOUT_SEC (no platform) now returns 400."""
        status, data = self._post(
            "/tuner/config/defaults",
            {"SCAN_TIMEOUT_SEC": 7.0, "BOARD_MAX_TEMP": 82},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("'platform' is required" in e for e in data["errors"]))

    def test_null_platform_now_rejected(self):
        """Explicit {platform: null} is no longer routed to legacy — returns 400."""
        status, data = self._post(
            "/tuner/config/defaults",
            {"platform": None, "BOARD_MAX_TEMP": 80},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("'platform' is required" in e for e in data["errors"]))


class TestConfigFleetOps(unittest.TestCase):
    """Tests for POST /tuner/config/fleet_ops (new endpoint)."""

    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.MINER_CONFIGS.clear()
        apply_defaults()

        import tuner_app.config.persistence as _p

        self._orig_save = _p.save_config_to_disk
        _p.save_config_to_disk = lambda: None

        from tuner_app.http_server.handlers import miners_routes as _mr

        self._orig_mr_save = _mr.save_config_to_disk
        _mr.save_config_to_disk = lambda: None

        self._stub = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"
        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        import tuner_app.config.persistence as _p

        _p.save_config_to_disk = self._orig_save

        from tuner_app.http_server.handlers import miners_routes as _mr

        _mr.save_config_to_disk = self._orig_mr_save

        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.MINER_CONFIGS.clear()

    def _post(self, path, body, cookie=None):
        req = request.Request(self.base + path, method="POST")
        req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        data = json.dumps(body).encode()
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except error.HTTPError as ex:
            return ex.code, json.loads(ex.read().decode())

    def test_writes_to_fleet_ops_bucket(self):
        status, data = self._post(
            "/tuner/config/fleet_ops",
            {"SCAN_TIMEOUT_SEC": 7.5},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_TIMEOUT_SEC"], 7.5)

    def test_rejects_per_platform_key(self):
        """BOARD_MAX_TEMP is a per-platform key — must be rejected from /fleet_ops."""
        status, data = self._post(
            "/tuner/config/fleet_ops",
            {"BOARD_MAX_TEMP": 80},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("not a fleet-ops key" in e for e in data["errors"]))

    def test_fleet_ops_endpoint_rejects_per_platform_mrr_modifier(self):
        """MRR_HASHRATE_MODIFIER_PCT lives in per-platform defaults, not fleet_ops.
        The fleet-ops endpoint must reject it with a clear error, so the dashboard's
        MRR card can't accidentally route a per-platform key to the wrong endpoint.

        The dashboard's submitMRRSettings() must POST this key to
        /tuner/config/defaults (once per platform) instead."""
        status, data = self._post(
            "/tuner/config/fleet_ops",
            {"MRR_HASHRATE_MODIFIER_PCT": 5.5},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertFalse(data["updated"])
        self.assertTrue(any("not a fleet-ops key" in e for e in data["errors"]))

    def test_password_derivation_from_scan_passwords(self):
        """Updating SCAN_PASSWORDS via /fleet_ops derives fleet_ops PASSWORD."""
        status, data = self._post(
            "/tuner/config/fleet_ops",
            {"SCAN_PASSWORDS": ["fleet_pw", "backup"]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_PASSWORDS"], ["fleet_pw", "backup"])
        self.assertEqual(state.CONFIG["fleet_ops"]["PASSWORD"], "fleet_pw")

    def test_invalid_fleet_ops_value_rejected(self):
        """Validation still runs — out-of-bounds SCAN_TIMEOUT_SEC should be rejected.
        Returns HTTP 200 with updated=False + errors (consistent with existing
        /defaults behavior — the endpoint never sends 4xx for validation errors)."""
        status, data = self._post(
            "/tuner/config/fleet_ops",
            {"SCAN_TIMEOUT_SEC": 9999},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertFalse(data["updated"])
        self.assertGreater(len(data["errors"]), 0)

    def test_multiple_fleet_ops_keys_in_one_request(self):
        """Multiple fleet-ops keys can be submitted together."""
        status, data = self._post(
            "/tuner/config/fleet_ops",
            {"SCAN_TIMEOUT_SEC": 5.0, "SCAN_CONCURRENCY": 8},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_TIMEOUT_SEC"], 5.0)
        self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_CONCURRENCY"], 8)

    def test_requires_auth(self):
        """Without a session cookie, /tuner/config/fleet_ops returns 401."""
        status, data = self._post(
            "/tuner/config/fleet_ops",
            {"SCAN_TIMEOUT_SEC": 5.0},
        )
        self.assertEqual(status, 401)


class TestBulkApplyConfigPlatformAware(unittest.TestCase):
    """Tests for POST /tuner/bulk/apply_config with new platform-aware shape."""

    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.MINER_CONFIGS.clear()
        apply_defaults()

        import tuner_app.config.persistence as _p

        self._orig_save = _p.save_config_to_disk
        _p.save_config_to_disk = lambda: None

        from tuner_app.http_server.handlers import bulk_routes as _br

        self._orig_br_save = _br.save_config_to_disk
        _br.save_config_to_disk = lambda: None

        self._stub = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"
        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        import tuner_app.config.persistence as _p

        _p.save_config_to_disk = self._orig_save

        from tuner_app.http_server.handlers import bulk_routes as _br

        _br.save_config_to_disk = self._orig_br_save

        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.MINER_CONFIGS.clear()

    def _post(self, path, body, cookie=None):
        req = request.Request(self.base + path, method="POST")
        req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        data = json.dumps(body).encode()
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, json.loads(resp.read().decode())
        except error.HTTPError as ex:
            return ex.code, json.loads(ex.read().decode())

    def _register_miner(self, mac, firmware_type="epic", ip=None):
        """Register a miner in MINER_CONFIGS in v4 shape with the given firmware."""
        if ip is None:
            ip = "10.0.0." + mac.split(":")[-1]
        with state.config_lock:
            state.MINER_CONFIGS[mac] = {
                "ip": ip,
                "current_firmware": firmware_type,
                "id_synthesized": False,
                "platforms": {firmware_type: {}},
            }

    def test_new_shape_skips_mismatched_platform(self):
        """3 miners: 2 epic + 1 bixbit. POST platform=epic → 2 ok, 1 platform_mismatch."""
        mac_a = "aa:bb:cc:dd:ee:01"
        mac_b = "aa:bb:cc:dd:ee:02"
        mac_c = "aa:bb:cc:dd:ee:03"
        self._register_miner(mac_a, "epic")
        self._register_miner(mac_b, "epic")
        self._register_miner(mac_c, "bixbit")

        status, data = self._post(
            "/tuner/bulk/apply_config",
            {
                "macs": [mac_a, mac_b, mac_c],
                "platform": "epic",
                "config": {"BOARD_MAX_TEMP": 80},
            },
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        results = data["results"]

        self.assertTrue(results[mac_a]["ok"])
        self.assertTrue(results[mac_b]["ok"])
        self.assertFalse(results[mac_c]["ok"])
        # Platform-mismatch detail is nested in the "detail" field.
        detail = results[mac_c]["detail"]
        self.assertEqual(detail["reason"], "platform_mismatch")
        self.assertEqual(detail["expected"], "epic")
        self.assertEqual(detail["actual"], "bixbit")

        # Override written only for the two matching miners — under platforms[platform].
        self.assertEqual(state.MINER_CONFIGS[mac_a]["platforms"]["epic"]["BOARD_MAX_TEMP"], 80)
        self.assertEqual(state.MINER_CONFIGS[mac_b]["platforms"]["epic"]["BOARD_MAX_TEMP"], 80)
        self.assertNotIn(
            "BOARD_MAX_TEMP",
            state.MINER_CONFIGS.get(mac_c, {}).get("platforms", {}).get("epic", {}),
        )

    def test_new_shape_miner_without_explicit_firmware_type_defaults_to_epic(self):
        """A miner with no MINER_CONFIGS entry defaults to 'epic' for platform matching."""
        mac = "aa:bb:cc:dd:ee:11"
        # No entry at all in MINER_CONFIGS — defaults to epic.
        status, data = self._post(
            "/tuner/bulk/apply_config",
            {"macs": [mac], "platform": "epic", "config": {"BOARD_MAX_TEMP": 83}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["results"][mac]["ok"])
        self.assertEqual(state.MINER_CONFIGS[mac]["platforms"]["epic"]["BOARD_MAX_TEMP"], 83)

    def test_new_shape_rejects_unknown_platform(self):
        mac = "aa:bb:cc:dd:ee:21"
        status, data = self._post(
            "/tuner/bulk/apply_config",
            {"macs": [mac], "platform": "bogus_fw", "config": {"BOARD_MAX_TEMP": 80}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertGreater(len(data["errors"]), 0)
        self.assertTrue(any("platform must be one of" in e for e in data["errors"]))

    def test_new_shape_invalid_config_returns_400(self):
        """Invalid config value (out of bounds) rejects the entire batch at 400."""
        mac = "aa:bb:cc:dd:ee:31"
        status, data = self._post(
            "/tuner/bulk/apply_config",
            {"macs": [mac], "platform": "epic", "config": {"BOARD_MAX_TEMP": 9999}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertGreater(len(data["errors"]), 0)
        # No BOARD_MAX_TEMP override written into the platform bucket.
        self.assertNotIn(
            "BOARD_MAX_TEMP",
            state.MINER_CONFIGS.get(mac, {}).get("platforms", {}).get("epic", {}),
        )

    def test_legacy_shape_without_platform_now_rejected(self):
        """Body with no 'platform' key is rejected with HTTP 400 (legacy path removed)."""
        mac_a = "aa:bb:cc:dd:ee:41"
        mac_b = "aa:bb:cc:dd:ee:42"
        self._register_miner(mac_a, "epic")
        self._register_miner(mac_b, "bixbit")

        status, data = self._post(
            "/tuner/bulk/apply_config",
            {"macs": [mac_a, mac_b], "config": {"CHIP_FREQ_SPREAD_MHZ": 50}},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        self.assertGreater(len(data["errors"]), 0)
        self.assertTrue(any("'platform' is required" in e for e in data["errors"]))
        # No overrides written.
        self.assertNotIn(
            "CHIP_FREQ_SPREAD_MHZ",
            state.MINER_CONFIGS.get(mac_a, {}).get("platforms", {}).get("epic", {}),
        )
        self.assertNotIn(
            "CHIP_FREQ_SPREAD_MHZ",
            state.MINER_CONFIGS.get(mac_b, {}).get("platforms", {}).get("bixbit", {}),
        )

    def test_new_shape_all_mismatch_bulk_result_shape_intact(self):
        """All miners mismatch → all skipped; summary counts are still correct."""
        mac_a = "aa:bb:cc:dd:ee:51"
        mac_b = "aa:bb:cc:dd:ee:52"
        self._register_miner(mac_a, "bixbit")
        self._register_miner(mac_b, "bixbit")

        status, data = self._post(
            "/tuner/bulk/apply_config",
            {
                "macs": [mac_a, mac_b],
                "platform": "epic",
                "config": {"BOARD_MAX_TEMP": 80},
            },
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        # Both skipped — reported as failures in the summary since ok=False.
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["failed"], 2)
        self.assertEqual(data["summary"]["succeeded"], 0)
        for mac in (mac_a, mac_b):
            self.assertFalse(data["results"][mac]["ok"])
            self.assertEqual(data["results"][mac]["detail"]["reason"], "platform_mismatch")


if __name__ == "__main__":
    unittest.main()
