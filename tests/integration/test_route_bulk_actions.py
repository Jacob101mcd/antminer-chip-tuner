"""Integration tests for the new fleet-wide bulk action endpoints.

Covers:
- /tuner/bulk/start_mining + /tuner/bulk/stop_mining (raw vendor cmds)
- /tuner/bulk/reboot
- /tuner/bulk/set_power_limit (capability-gated; ePIC reports unsupported)
- /tuner/bulk/mrr_resync (intent derived from engine phase)
- /tuner/bulk/retune_voltage (uses each engine's active_sweep_voltage_mv)
- Legacy {ips:...} body shape rejected with HTTP 400 by every new endpoint
"""

from __future__ import annotations

import json
import threading
import unittest
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server

_MAC_EPIC = "aa:bb:cc:dd:ee:01"
_MAC_BIXBIT = "aa:bb:cc:dd:ee:02"


class _FakeAPI:
    def __init__(self, has_power_limit=True, raise_on=None):
        self._has_power_limit = has_power_limit
        self._raise_on = raise_on or {}
        self.calls = []

    def _maybe_raise(self, name):
        if name in self._raise_on:
            raise self._raise_on[name]

    def start_mining(self):
        self.calls.append(("start_mining",))
        self._maybe_raise("start_mining")

    def stop_mining(self):
        self.calls.append(("stop_mining",))
        self._maybe_raise("stop_mining")

    def reboot(self, delay=0):
        self.calls.append(("reboot", delay))
        self._maybe_raise("reboot")

    def has_external_power_limit(self):
        return self._has_power_limit

    def set_power_limit(self, watts):
        self.calls.append(("set_power_limit", watts))
        self._maybe_raise("set_power_limit")


class _FakeEngine:
    """Engine double exposing the attributes the bulk action factories read."""

    PHASE_PERPETUAL = "PHASE_PERPETUAL"
    PHASE_STOPPED = "PHASE_STOPPED"
    PHASE_IDLE = "PHASE_IDLE"
    PHASE_ERROR = "PHASE_ERROR"

    def __init__(self, api, phase="PHASE_PERPETUAL", active_voltage=14000):
        self.api = api
        self.phase = phase
        self.active_sweep_voltage_mv = active_voltage
        self.mrr_last_sync = None
        self.sync_calls = []

    def _mrr_sync(self, intent, reason=""):
        self.sync_calls.append((intent, reason))
        self.mrr_last_sync = {"intent": intent, "reason": reason}


class _StubManager:
    def __init__(self):
        self.engines = {}
        self.retune_calls = []
        self.retune_returns = {}

    def get_engine(self, identifier):
        return self.engines[identifier]

    def retune_voltage(self, mac, voltage_mv):
        self.retune_calls.append((mac, voltage_mv))
        return self.retune_returns.get(mac, (True, ""))


class TestBulkActionRoutes(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.MINER_CONFIGS.clear()

        self._stub_manager = _StubManager()
        self._epic_api = _FakeAPI(has_power_limit=False)
        self._bixbit_api = _FakeAPI(has_power_limit=True)
        self._stub_manager.engines[_MAC_EPIC] = _FakeEngine(self._epic_api, phase="PHASE_PERPETUAL")
        self._stub_manager.engines[_MAC_BIXBIT] = _FakeEngine(
            self._bixbit_api, phase="PHASE_STOPPED", active_voltage=14250
        )

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

    def _post(self, path, body=None):
        req = request.Request(self.base + path, method="POST")
        req.add_header("Cookie", self.cookie)
        data = None
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        try:
            resp = request.urlopen(req, data=data, timeout=5)
            return resp.status, json.loads(resp.read().decode() or "null")
        except error.HTTPError as ex:
            body_text = ex.read().decode() or "null"
            try:
                return ex.code, json.loads(body_text)
            except json.JSONDecodeError:
                return ex.code, body_text

    # ── start_mining / stop_mining ────────────────────────────────────────

    def test_bulk_start_mining_calls_api_per_miner(self):
        status, body = self._post("/tuner/bulk/start_mining", {"macs": [_MAC_EPIC, _MAC_BIXBIT]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"], {"total": 2, "succeeded": 2, "failed": 0})
        self.assertEqual(self._epic_api.calls, [("start_mining",)])
        self.assertEqual(self._bixbit_api.calls, [("start_mining",)])

    def test_bulk_stop_mining_calls_api_per_miner(self):
        status, body = self._post("/tuner/bulk/stop_mining", {"macs": [_MAC_EPIC, _MAC_BIXBIT]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["succeeded"], 2)
        self.assertEqual(self._epic_api.calls, [("stop_mining",)])
        self.assertEqual(self._bixbit_api.calls, [("stop_mining",)])

    def test_bulk_start_mining_offline_does_not_fail_others(self):
        from tuner_app.miner.exceptions import MinerOfflineError

        self._epic_api._raise_on = {"start_mining": MinerOfflineError("offline")}
        status, body = self._post("/tuner/bulk/start_mining", {"macs": [_MAC_EPIC, _MAC_BIXBIT]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"], {"total": 2, "succeeded": 1, "failed": 1})
        self.assertFalse(body["results"][_MAC_EPIC]["ok"])
        self.assertIn("offline", body["results"][_MAC_EPIC]["error"])
        self.assertTrue(body["results"][_MAC_BIXBIT]["ok"])

    def test_bulk_start_mining_rejects_legacy_ips_body(self):
        status, body = self._post("/tuner/bulk/start_mining", {"ips": ["10.0.0.1"]})
        self.assertEqual(status, 400)
        self.assertTrue(any("'ips' body field is no longer accepted" in e for e in body["errors"]))

    # ── reboot ────────────────────────────────────────────────────────────

    def test_bulk_reboot_calls_api_with_zero_delay(self):
        status, body = self._post("/tuner/bulk/reboot", {"macs": [_MAC_EPIC, _MAC_BIXBIT]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["succeeded"], 2)
        self.assertEqual(self._epic_api.calls, [("reboot", 0)])
        self.assertEqual(self._bixbit_api.calls, [("reboot", 0)])

    # ── set_power_limit ──────────────────────────────────────────────────

    def test_bulk_set_power_limit_calls_api_when_capability_supported(self):
        status, body = self._post(
            "/tuner/bulk/set_power_limit", {"macs": [_MAC_BIXBIT], "watts": 3500}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["succeeded"], 1)
        self.assertEqual(self._bixbit_api.calls, [("set_power_limit", 3500)])

    def test_bulk_set_power_limit_skips_miners_without_capability(self):
        status, body = self._post(
            "/tuner/bulk/set_power_limit",
            {"macs": [_MAC_EPIC, _MAC_BIXBIT], "watts": 3500},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["succeeded"], 1)
        self.assertEqual(body["summary"]["failed"], 1)
        self.assertFalse(body["results"][_MAC_EPIC]["ok"])
        self.assertIn("capability_unsupported", body["results"][_MAC_EPIC]["error"])
        self.assertEqual(self._epic_api.calls, [])
        self.assertEqual(self._bixbit_api.calls, [("set_power_limit", 3500)])

    def test_bulk_set_power_limit_validates_watts_bounds(self):
        for bad_watts in (None, "not_a_number", 0, 100, 12000):
            with self.subTest(watts=bad_watts):
                status, body = self._post(
                    "/tuner/bulk/set_power_limit",
                    {"macs": [_MAC_BIXBIT], "watts": bad_watts},
                )
                self.assertEqual(status, 400)
                self.assertGreater(len(body["errors"]), 0)

    # ── mrr_resync ──────────────────────────────────────────────────────

    def test_bulk_mrr_resync_uses_engine_phase_intent(self):
        status, body = self._post("/tuner/bulk/mrr_resync", {"macs": [_MAC_EPIC, _MAC_BIXBIT]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["succeeded"], 2)
        epic_engine = self._stub_manager.engines[_MAC_EPIC]
        bixbit_engine = self._stub_manager.engines[_MAC_BIXBIT]
        # PHASE_PERPETUAL → maintaining; PHASE_STOPPED → stopped.
        self.assertEqual(epic_engine.sync_calls[0][0], "maintaining")
        self.assertEqual(bixbit_engine.sync_calls[0][0], "stopped")

    # ── retune_voltage ───────────────────────────────────────────────────

    def test_bulk_retune_voltage_uses_active_voltage_per_engine(self):
        status, body = self._post("/tuner/bulk/retune_voltage", {"macs": [_MAC_EPIC, _MAC_BIXBIT]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["succeeded"], 2)
        # Each engine's active voltage flows through to the manager call.
        self.assertEqual(
            sorted(self._stub_manager.retune_calls),
            sorted([(_MAC_EPIC, 14000), (_MAC_BIXBIT, 14250)]),
        )

    def test_bulk_retune_voltage_fails_when_no_active_voltage(self):
        self._stub_manager.engines[_MAC_EPIC].active_sweep_voltage_mv = None
        status, body = self._post("/tuner/bulk/retune_voltage", {"macs": [_MAC_EPIC]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["failed"], 1)
        self.assertIn("no active voltage", body["results"][_MAC_EPIC]["error"])

    def test_bulk_retune_voltage_propagates_engine_refusal(self):
        self._stub_manager.retune_returns[_MAC_EPIC] = (False, "engine is busy")
        status, body = self._post("/tuner/bulk/retune_voltage", {"macs": [_MAC_EPIC]})
        self.assertEqual(status, 200)
        self.assertEqual(body["summary"]["failed"], 1)
        self.assertIn("engine is busy", body["results"][_MAC_EPIC]["error"])

    # ── legacy body shape rejected by every new endpoint ────────────────

    def test_legacy_ips_body_rejected_by_each_new_endpoint(self):
        for path in (
            "/tuner/bulk/stop_mining",
            "/tuner/bulk/reboot",
            "/tuner/bulk/set_power_limit",
            "/tuner/bulk/mrr_resync",
            "/tuner/bulk/retune_voltage",
        ):
            with self.subTest(path=path):
                body_kwargs = {"ips": ["10.0.0.1"]}
                if path.endswith("set_power_limit"):
                    body_kwargs["watts"] = 3500
                status, body = self._post(path, body_kwargs)
                self.assertEqual(status, 400, msg=f"{path} did not 400 on legacy body")
                self.assertTrue(
                    any("'ips' body field is no longer accepted" in e for e in body["errors"]),
                    msg=f"{path} did not surface legacy-ips message",
                )
