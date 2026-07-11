"""Integration tests for /tuner/minerstat/fetch_now and the deleted profit endpoints.

After the Recompute & Apply UI was retired, /tuner/minerstat/fetch_now
calls the shared ``apply_profit_recompute`` helper after a successful
snapshot save and returns ``auto_apply: {applied, skipped, failures}`` in
the response body. The /tuner/profit/recompute_preview and
/tuner/profit/apply endpoints were deleted entirely.
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest import mock
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server


class _StubManager:
    def __init__(self):
        self.engines = {}

    def compute_profit_preview(self, ips):
        return {"miners": []}

    def apply_profit_action(self, ip, action, voltage_mv, freq_mhz=None):
        return (True, "", {})


class TestMinerstatRoutes(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.CONFIG.setdefault("fleet_ops", {})
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ["10.0.0.1"]
        state.CONFIG["fleet_ops"]["API_PORT"] = 4028
        state.CONFIG["fleet_ops"]["MINERSTAT_COIN"] = "BTC"
        state.CONFIG["fleet_ops"]["MINERSTAT_API_KEY"] = ""
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

    def _request(self, method, path, body=None):
        req = request.Request(self.base + path, method=method)
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        req.add_header("Cookie", self.cookie)
        try:
            resp = request.urlopen(req, data=data, timeout=2)
            return resp.status, json.loads(resp.read().decode() or "null")
        except error.HTTPError as ex:
            body_text = ex.read().decode() or "null"
            try:
                return ex.code, json.loads(body_text)
            except json.JSONDecodeError:
                return ex.code, body_text

    def test_recompute_preview_endpoint_returns_404(self):
        status, _body = self._request("POST", "/tuner/profit/recompute_preview", body={})
        self.assertEqual(status, 404)

    def test_profit_apply_endpoint_returns_404(self):
        status, _body = self._request("POST", "/tuner/profit/apply", body={"ips": ["10.0.0.1"]})
        self.assertEqual(status, 404)

    def test_fetch_now_triggers_auto_apply_and_returns_summary(self):
        from tuner_app.http_server.handlers import minerstat_routes as mr

        with (
            mock.patch.object(mr, "fetch_minerstat_coins", return_value={"BTC": {"price": 50000}}),
            mock.patch.object(mr, "save_minerstat_snapshot", return_value={"snapshot": "ok"}),
            mock.patch.object(
                mr,
                "apply_profit_recompute",
                return_value={"applied": 2, "skipped": 1, "failures": []},
            ) as m_apply,
        ):
            status, body = self._request("POST", "/tuner/minerstat/fetch_now")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["auto_apply"], {"applied": 2, "skipped": 1, "failures": []})
        m_apply.assert_called_once_with(self._stub_manager)

    def test_fetch_now_skips_auto_apply_on_fetch_failure(self):
        from tuner_app.http_server.handlers import minerstat_routes as mr
        from tuner_app.profit.minerstat import MinerstatError

        with (
            mock.patch.object(
                mr, "fetch_minerstat_coins", side_effect=MinerstatError("upstream 502")
            ),
            mock.patch.object(mr, "save_minerstat_snapshot") as m_save,
            mock.patch.object(mr, "apply_profit_recompute") as m_apply,
        ):
            status, body = self._request("POST", "/tuner/minerstat/fetch_now")
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        m_save.assert_not_called()
        m_apply.assert_not_called()
