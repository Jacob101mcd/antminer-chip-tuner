"""Integration tests for the status route handlers.

Covers:
- /tuner/status returns the manager.get_all_status() payload as JSON
- /tuner/overview returns the manager.get_overview() payload as JSON
- /tuner/config returns {defaults, miner_configs} from state
"""

from __future__ import annotations

import copy
import json
import threading
import unittest
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server


class _StubManager:
    """Stub manager exposing canned status / overview payloads."""

    def __init__(self):
        self.engines = {}
        self._all_status = {
            "192.168.1.1": {
                "phase": "PHASE_PERPETUAL",
                "phase_detail": "monitoring",
                "tuning_complete": True,
                "voltage_results": [],
                "vf_surface": [],
            }
        }
        self._overview = {
            "total_hashrate_ths": 100.0,
            "total_power_w": 3000.0,
            "avg_efficiency_jth": 30.0,
            "total_profit_usd_day": None,
            "state_counts": {"idle": 0, "tuning": 0, "maintaining": 1, "error": 0, "stopped": 0},
            "mining_counts": {"mining": 1, "stopped": 0, "unknown": 0},
            "miners": [],
        }

    def get_all_status(self):
        return self._all_status

    def get_overview(self):
        return self._overview


class TestStatusRoutes(unittest.TestCase):
    def setUp(self):
        self._config_snapshot = copy.deepcopy(state.CONFIG)
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.CONFIG["fleet_ops"].setdefault("MINER_IPS", [])
        state.CONFIG["fleet_ops"].setdefault("API_PORT", 4028)
        state.MINER_CONFIGS.clear()

        self._stub_manager = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub_manager)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"

        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.MINER_CONFIGS.clear()
        state.CONFIG.clear()
        state.CONFIG.update(self._config_snapshot)

    def _request(self, method, path, body=None, cookie=None):
        req = request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, resp.read().decode()
        except error.HTTPError as ex:
            return ex.code, ex.read().decode()

    def test_status_returns_manager_payload(self):
        status, body = self._request("GET", "/tuner/status", cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("192.168.1.1", data)
        self.assertEqual(data["192.168.1.1"]["phase"], "PHASE_PERPETUAL")

    def test_overview_returns_manager_payload(self):
        status, body = self._request("GET", "/tuner/overview", cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total_hashrate_ths"], 100.0)
        self.assertEqual(data["total_power_w"], 3000.0)
        self.assertEqual(data["state_counts"]["maintaining"], 1)

    def test_config_returns_defaults_and_overrides(self):
        # Use a sentinel value we control, not the live CONFIG.
        # Write to fleet_ops so it's accessible in the v3 CONFIG structure.
        state.CONFIG["fleet_ops"]["__TEST_KEY__"] = "test_value"
        state.MINER_CONFIGS["10.0.0.5"] = {"CHIP_FREQ_SPREAD_MHZ": 50}

        status, body = self._request("GET", "/tuner/config", cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)

        # Top-level shape: "defaults" (per-platform sub-dict), "fleet_ops",
        # "miner_configs".  The old doubly-nested shape
        # (data["defaults"]["defaults"]["epic"]) is now the wrong shape —
        # "defaults" must be the per-platform mapping directly.
        self.assertIn("defaults", data)
        self.assertIn("fleet_ops", data)
        self.assertIn("miner_configs", data)

        # "defaults" must be the per-platform mapping, not the raw state.CONFIG
        for platform in ("epic", "bixbit", "luxos", "braiins"):
            self.assertIn(
                platform,
                data["defaults"],
                f"defaults must contain platform key '{platform}'",
            )

        # fleet-ops sentinel must be at the top-level "fleet_ops" key
        self.assertEqual(data["fleet_ops"]["__TEST_KEY__"], "test_value")

        # per-miner override accessible via "miner_configs"
        self.assertEqual(data["miner_configs"]["10.0.0.5"]["CHIP_FREQ_SPREAD_MHZ"], 50)

        del state.CONFIG["fleet_ops"]["__TEST_KEY__"]

    def test_config_omits_all_credentials(self):
        secrets = {
            "PASSWORD": "miner-password-value",
            "SCAN_PASSWORDS": ["scan-password-value"],
            "MRR_API_KEY": "mrr-key-value",
            "MRR_API_SECRET": "mrr-secret-value",
            "MINERSTAT_API_KEY": "minerstat-key-value",
        }
        state.CONFIG["fleet_ops"].update(secrets)
        state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"] = {
            "ip": "192.0.2.20",
            "PASSWORD": "per-miner-password-value",
            "platforms": {"epic": {}},
        }

        status, body = self._request("GET", "/tuner/config", cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        serialized = json.dumps(data)

        for key, value in secrets.items():
            self.assertNotIn(key, serialized)
            for secret in value if isinstance(value, list) else [value]:
                self.assertNotIn(secret, serialized)
        self.assertNotIn("per-miner-password-value", serialized)
        self.assertEqual(
            data["miner_configs"]["aa:bb:cc:dd:ee:01"]["ip"],
            "192.0.2.20",
        )

    def test_status_requires_auth(self):
        status, body = self._request("GET", "/tuner/status")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
