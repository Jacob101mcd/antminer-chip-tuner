"""Integration tests for GET /tuner/metrics/<mac> (Phase B / B11)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server
from tuner_app.metrics.store import MetricsStore


class _StubManager:
    def __init__(self):
        self.engines = {}


class TestMetricsEndpoint(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.CONFIG["fleet_ops"].setdefault("MINER_IPS", [])
        state.CONFIG["fleet_ops"].setdefault("API_PORT", 4028)
        state.MINER_CONFIGS.clear()

        self.tmpdir = tempfile.TemporaryDirectory()
        self._saved_metrics_store = state.metrics_store
        state.metrics_store = MetricsStore(os.path.join(self.tmpdir.name, "metrics.db"))

        self._stub_manager = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub_manager)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"

        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"
        self.mac = "aa:bb:cc:dd:ee:ff"
        self.mac_dashes = "aa-bb-cc-dd-ee-ff"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if state.metrics_store is not None:
            state.metrics_store.stop()
        state.metrics_store = self._saved_metrics_store
        self.tmpdir.cleanup()
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.MINER_CONFIGS.clear()

    def _get(self, path: str, cookie: str | None = None) -> tuple[int, str]:
        req = request.Request(self.base + path, method="GET")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            resp = request.urlopen(req, timeout=5)
            return resp.status, resp.read().decode()
        except error.HTTPError as ex:
            return ex.code, ex.read().decode()

    def test_requires_auth(self):
        status, _body = self._get(f"/tuner/metrics/{self.mac_dashes}")
        self.assertEqual(status, 401)

    def test_unknown_mac_returns_empty_series(self):
        # No data has been recorded — endpoint still returns a valid envelope.
        status, body = self._get(f"/tuner/metrics/{self.mac_dashes}", cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["mac"], self.mac)
        self.assertIn("hashrate_ths", data["series"])
        self.assertEqual(data["series"]["hashrate_ths"]["avg"], [])

    def test_default_range_is_24h(self):
        status, body = self._get(f"/tuner/metrics/{self.mac_dashes}", cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        # Default range is 24h → window of ~86400 seconds.  Allow some slack
        # for clock progression between request and response.
        window = data["to"] - data["from"]
        self.assertAlmostEqual(window, 86400.0, delta=10.0)

    def test_returns_recorded_samples(self):
        # Record a few samples directly through the store, then query.
        now = time.time()
        for i in range(3):
            state.metrics_store.record_sample(
                self.mac,
                {
                    "ts": now - 60 * (3 - i),
                    "hashrate_ths": 200.0 + i,
                    "power_w": 4200.0,
                    "efficiency_jth": 21.0,
                    "temp_max_c": 72.0,
                    "fan_speed": 50,
                    "firmware_type": "epic",
                },
            )
        status, body = self._get(
            f"/tuner/metrics/{self.mac_dashes}?range=1h&metrics=hashrate_ths",
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        avg_pts = data["series"]["hashrate_ths"]["avg"]
        # Three samples → at most three points (depends on bucket alignment).
        self.assertGreaterEqual(len(avg_pts), 1)
        self.assertLessEqual(len(avg_pts), 3)

    def test_unknown_range_returns_400(self):
        status, body = self._get(
            f"/tuner/metrics/{self.mac_dashes}?range=99y",
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertFalse(data["ok"])

    def test_custom_range_requires_from_to(self):
        status, body = self._get(
            f"/tuner/metrics/{self.mac_dashes}?range=custom",
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertIn("'from'", data["error"])

    def test_custom_range_valid_returns_200(self):
        status, body = self._get(
            f"/tuner/metrics/{self.mac_dashes}?range=custom&from=1000&to=2000",
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["from"], 1000.0)
        self.assertEqual(data["to"], 2000.0)

    def test_invalid_mac_in_path_returns_400(self):
        status, body = self._get("/tuner/metrics/not-a-mac", cookie=self.cookie)
        self.assertEqual(status, 400)
        data = json.loads(body)
        self.assertIn("invalid MAC", data["error"])


if __name__ == "__main__":
    unittest.main()
