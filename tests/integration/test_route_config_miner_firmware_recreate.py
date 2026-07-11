"""Integration tests for the firmware-change engine teardown side effect on
POST /tuner/config/miner/{mac}.

Issue #25: when an operator mutates the per-miner ``current_firmware`` (e.g.
from "bixbit" to "luxos"), the running TuningEngine instance still holds the
old firmware's MinerAPI subclass (BixbitMinerAPI). Capabilities reported via
/tuner/status reflect the stale class until process restart. The fix is to
tear down the existing engine via the canonical destroy() + pop_engine()
sequence so the next manager.get_engine(mac) lazily instantiates fresh with
the new firmware.

Post-A12 cutover: the URL path uses dashed MAC; the wire shape still accepts
``firmware_type`` as an operator-facing alias that is translated to
``current_firmware`` on write per the v4 schema.

Covers:
- firmware change tears down the existing engine (destroy() called, engine
  removed from manager.engines).
- Same firmware as currently registered is a no-op (no destroy(), engine
  remains in registry — avoids gratuitous teardown + flicker).
- Body without firmware doesn't trigger teardown (existing per-miner config
  writes like MRR_RIG_ID continue to work unchanged).
- Engine not yet created (first config write before any get_engine call) is
  handled gracefully — no AttributeError, response 200.
- firmware combined with other side-effecting keys (e.g. MRR_RIG_ID): both
  side effects fire (engine torn down AND MRR pool re-pushed when the next
  get_engine creates the fresh engine).
"""

from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import MagicMock
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.config.defaults import apply_defaults
from tuner_app.constants import _mac_for_filename
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server


class _StubManager:
    """Manager stub exposing the lifecycle primitives the handler uses:
    get_engine, peek_engine, pop_engine. Records pop_engine() calls.

    Engines dict is keyed by canonical MAC in v4.
    """

    def __init__(self):
        self.engines = {}
        self.popped = []
        # Tracks engines created by get_engine() lazily — used to verify that
        # downstream MRR pool push goes through the fresh engine after teardown.
        self.created_engines = []

    def get_engine(self, identifier):
        if identifier not in self.engines:
            new_engine = MagicMock(name=f"engine-{identifier}")
            new_engine.mac = identifier
            self.engines[identifier] = new_engine
            self.created_engines.append(new_engine)
        return self.engines[identifier]

    def peek_engine(self, identifier):
        return self.engines.get(identifier)

    def pop_engine(self, identifier):
        self.popped.append(identifier)
        return self.engines.pop(identifier, None)


def _v4_entry(ip, firmware):
    return {
        "ip": ip,
        "current_firmware": firmware,
        "id_synthesized": False,
        "platforms": {firmware: {}},
    }


def _path(mac):
    return "/tuner/config/miner/" + _mac_for_filename(mac)


class TestConfigMinerFirmwareTypeRecreate(unittest.TestCase):
    """The five required cases from the brief (post-A12 MAC URL paths)."""

    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        state.MINER_CONFIGS.clear()
        # Re-apply defaults so CONFIG (especially defaults["epic"] etc.) is in
        # a known clean state for each test.
        apply_defaults()

        # Patch save_config_to_disk in BOTH modules — handler uses miners_routes
        # which imports it directly; persistence module is the source.
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

    # ── 1. firmware change tears down engine ────────────────────────────

    def test_firmware_type_change_tears_down_engine(self):
        """bixbit → luxos: the bixbit-instantiated engine must be destroyed and
        popped from the manager registry so the next get_engine() lazily creates
        a fresh one bound to LuxosMinerAPI."""
        mac = "aa:bb:cc:dd:ee:01"
        state.MINER_CONFIGS[mac] = _v4_entry("10.0.0.1", "bixbit")
        old_engine = MagicMock(name="old-engine-bixbit")
        old_engine.mac = mac
        self._stub.engines[mac] = old_engine

        status, data = self._post(
            _path(mac),
            {"firmware_type": "luxos"},
            cookie=self.cookie,
        )

        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        old_engine.destroy.assert_called_once()
        self.assertIn(mac, self._stub.popped)
        self.assertNotIn(old_engine, self._stub.engines.values())
        # The new firmware was persisted to MINER_CONFIGS under the v4 key.
        self.assertEqual(state.MINER_CONFIGS[mac]["current_firmware"], "luxos")

    # ── 2. Same firmware — no teardown ──────────────────────────────────

    def test_same_firmware_type_no_teardown(self):
        """Setting firmware to its current value must NOT tear down the
        engine. Avoids gratuitous flicker when an operator clicks "save" in
        the per-miner config tab without changing the type."""
        mac = "aa:bb:cc:dd:ee:02"
        state.MINER_CONFIGS[mac] = _v4_entry("10.0.0.2", "bixbit")
        existing_engine = MagicMock(name="existing-engine-bixbit")
        existing_engine.mac = mac
        self._stub.engines[mac] = existing_engine

        status, data = self._post(
            _path(mac),
            {"firmware_type": "bixbit"},
            cookie=self.cookie,
        )

        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        existing_engine.destroy.assert_not_called()
        self.assertNotIn(mac, self._stub.popped)
        self.assertIs(self._stub.engines[mac], existing_engine)

    # ── 3. Body without firmware — no teardown ──────────────────────────

    def test_body_without_firmware_type_no_teardown(self):
        """Other per-miner config writes (e.g. CHIP_FREQ_SPREAD_MHZ, an epic
        per-platform key) must NOT trigger an engine teardown. This is the
        regression path — every existing per-miner write must continue to
        work unchanged."""
        mac = "aa:bb:cc:dd:ee:03"
        state.MINER_CONFIGS[mac] = _v4_entry("10.0.0.3", "epic")
        existing_engine = MagicMock(name="existing-engine-epic")
        existing_engine.mac = mac
        self._stub.engines[mac] = existing_engine

        # CHIP_FREQ_SPREAD_MHZ is a valid per-platform key for epic — bounds
        # are wide enough that 50 should validate cleanly.
        status, data = self._post(
            _path(mac),
            {"CHIP_FREQ_SPREAD_MHZ": 50},
            cookie=self.cookie,
        )

        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        existing_engine.destroy.assert_not_called()
        self.assertNotIn(mac, self._stub.popped)
        self.assertIs(self._stub.engines[mac], existing_engine)
        # The override was persisted to the platform bucket.
        self.assertEqual(state.MINER_CONFIGS[mac]["platforms"]["epic"]["CHIP_FREQ_SPREAD_MHZ"], 50)

    # ── 4. Engine not yet created — no error ─────────────────────────────

    def test_firmware_type_change_with_no_engine_yet(self):
        """Operator changes firmware before any engine has been created. Must
        not raise; response 200; MINER_CONFIGS persisted. The handler may still
        call pop_engine() — that's a no-op when no engine is registered."""
        mac = "aa:bb:cc:dd:ee:04"
        state.MINER_CONFIGS[mac] = _v4_entry("10.0.0.4", "bixbit")
        # Note: NOT pre-populating self._stub.engines[mac]

        status, data = self._post(
            _path(mac),
            {"firmware_type": "luxos"},
            cookie=self.cookie,
        )

        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        self.assertNotIn(mac, self._stub.engines)
        self.assertEqual(state.MINER_CONFIGS[mac]["current_firmware"], "luxos")

    # ── 5. firmware combined with other side-effecting keys ─────────────

    def test_firmware_type_with_mrr_rig_id_both_side_effects_fire(self):
        """firmware AND MRR_RIG_ID in one body: engine teardown happens AND
        the MRR pool re-push runs (against the freshly-instantiated engine,
        not the destroyed one)."""
        mac = "aa:bb:cc:dd:ee:05"
        state.MINER_CONFIGS[mac] = _v4_entry("10.0.0.5", "bixbit")
        old_engine = MagicMock(name="old-engine-bixbit-with-mrr")
        old_engine.mac = mac
        self._stub.engines[mac] = old_engine

        status, data = self._post(
            _path(mac),
            {"firmware_type": "luxos", "MRR_RIG_ID": 1234},
            cookie=self.cookie,
        )

        self.assertEqual(status, 200)
        self.assertTrue(data["updated"])
        # Engine teardown happened.
        old_engine.destroy.assert_called_once()
        self.assertIn(mac, self._stub.popped)
        old_engine._mrr_apply_pool_config.assert_not_called()
        # A fresh engine was created via get_engine() during the MRR side
        # effect, and its _mrr_apply_pool_config was invoked.
        self.assertEqual(len(self._stub.created_engines), 1)
        fresh = self._stub.created_engines[0]
        self.assertIsNot(fresh, old_engine)
        fresh._mrr_apply_pool_config.assert_called_once()
        # Both config mutations persisted.
        self.assertEqual(state.MINER_CONFIGS[mac]["current_firmware"], "luxos")
        self.assertEqual(state.MINER_CONFIGS[mac]["MRR_RIG_ID"], 1234)


if __name__ == "__main__":
    unittest.main()
