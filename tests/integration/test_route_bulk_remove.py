"""Integration tests for /tuner/bulk/remove and a regression for the
single-miner /tuner/miners/remove path after both routes were refactored to
share `_remove_miner` in `tuner_app.manager.bulk`.

Covers (post-A12 MAC cutover):
- /tuner/bulk/remove returns the canonical {results, summary} bulk shape, MAC-keyed.
- Each successful removal drops the ip from MINER_IPS, drops the MINER_CONFIGS[mac]
  entry, calls manager.pop_engine(mac), calls engine.destroy() + thread.join,
  AND deletes the per-platform tuning files (.profile.json / .checkpoint.json /
  .stock.json under {mac}.{firmware} naming) plus the cross-platform .log.jsonl.
- One bad MAC doesn't abort the batch; partial-success accounting works.
- Single-miner /tuner/miners/remove still wipes the per-platform + legacy files.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from urllib import error, request

from tuner_app import state
from tuner_app.auth.passwords import hash_password
from tuner_app.auth.sessions import issue_session
from tuner_app.constants import _miner_data_path, _miner_platform_path
from tuner_app.http_server.handler import TunerHandler
from tuner_app.http_server.server import start_http_server


class _FakeThread:
    def __init__(self, alive=False):
        self._alive = alive
        self.joined = False

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.joined = True
        self._alive = False


class _FakeEngine:
    """Engine stub that records destroy() + join calls; never spawns threads."""

    def __init__(self, mac, ip="10.0.0.1", *, alive=False, destroy_raises=None):
        self.mac = mac
        self.ip = ip
        self.thread = _FakeThread(alive=alive)
        self._destroyed = False
        self._destroy_raises = destroy_raises

    def destroy(self):
        self._destroyed = True
        if self._destroy_raises is not None:
            raise self._destroy_raises


class _StubManager:
    """Manager stub with engines dict + pop_engine() recording.

    Mirrors the v4 ``TunerManager`` API: identifier resolution accepts MAC or
    IP via a thin reverse-lookup over ``state.MINER_CONFIGS``. The dict is
    keyed by MAC.
    """

    def __init__(self):
        self.engines = {}
        self.popped = []

    def _to_mac(self, identifier):
        if not isinstance(identifier, str):
            return identifier
        if "." in identifier and ":" not in identifier:
            for mac, ov in state.MINER_CONFIGS.items():
                if isinstance(ov, dict) and ov.get("ip") == identifier:
                    return mac
        return identifier

    def pop_engine(self, identifier):
        mac = self._to_mac(identifier)
        self.popped.append(mac)
        return self.engines.pop(mac, None)


_MAC_A = "aa:bb:cc:dd:ee:01"
_MAC_B = "aa:bb:cc:dd:ee:02"
_MAC_UNKNOWN = "aa:bb:cc:dd:ee:99"
_FW = "epic"


class TestBulkRemove(unittest.TestCase):
    PER_PLATFORM_SUFFIXES = (".profile.json", ".checkpoint.json", ".stock.json")
    LEGACY_SUFFIXES = (".json", ".checkpoint.json", ".stock.json", ".log.jsonl")

    def setUp(self):
        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update(
            {"password_hash": hash_password("test"), "created_at": "2026-01-01T00:00:00Z"}
        )
        # Snapshot then reset MINER_IPS / MINER_CONFIGS so tests are independent.
        self._orig_miner_ips = list(state.CONFIG["fleet_ops"].get("MINER_IPS", []))
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []
        state.MINER_CONFIGS.clear()

        # Patch save_config_to_disk in BOTH modules (manager.bulk uses it
        # directly now, miners_routes still uses it for config_defaults /
        # config_miner) so the tests don't write to disk.
        import tuner_app.config.persistence as persistence_mod
        import tuner_app.manager.bulk as bulk_mod

        self._orig_save = persistence_mod.save_config_to_disk
        self._orig_bulk_save = bulk_mod.save_config_to_disk
        persistence_mod.save_config_to_disk = lambda: None
        bulk_mod.save_config_to_disk = lambda: None

        self._stub_manager = _StubManager()
        self.server = start_http_server("localhost", 0, TunerHandler, self._stub_manager)
        port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://localhost:{port}"

        self.token = issue_session()
        self.cookie = f"tuner_session={self.token}"

        # Track files we create so tearDown can clean up if a test fails
        # mid-flight.
        self._created_files = []

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

        import tuner_app.config.persistence as persistence_mod
        import tuner_app.manager.bulk as bulk_mod

        persistence_mod.save_config_to_disk = self._orig_save
        bulk_mod.save_config_to_disk = self._orig_bulk_save

        for f in self._created_files:
            if os.path.exists(f):
                os.remove(f)

        state._sessions.clear()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": None, "created_at": None})
        state.CONFIG["fleet_ops"]["MINER_IPS"] = self._orig_miner_ips
        state.MINER_CONFIGS.clear()

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

    def _register(self, mac, ip, *, firmware=_FW):
        """Seed a v4-shape MINER_CONFIGS entry."""
        state.CONFIG["fleet_ops"]["MINER_IPS"].append(ip)
        state.MINER_CONFIGS[mac] = {
            "ip": ip,
            "current_firmware": firmware,
            "id_synthesized": False,
            "platforms": {firmware: {}},
        }

    def _seed_files_for(self, mac, ip, firmware=_FW):
        """Create per-platform + legacy files so we can assert they're gone.

        Per-platform: tuning_data/{mac-dashes}.{firmware}{suffix} for the three
        tuning artifacts plus the cross-platform .log.jsonl at
        tuning_data/{mac-dashes}.log.jsonl. Legacy IP-keyed files are also
        seeded since ``_remove_miner`` sweeps both naming schemes.
        """
        # Per-platform tuning files
        for suffix in self.PER_PLATFORM_SUFFIXES:
            path = _miner_platform_path(mac, firmware, suffix)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("{}")
            self._created_files.append(path)
        # Cross-platform log
        log_path = _miner_data_path(mac, ".log.jsonl")
        with open(log_path, "w") as f:
            f.write("")
        self._created_files.append(log_path)
        # Legacy IP-keyed orphans (the sweep should clean these too)
        for suffix in self.LEGACY_SUFFIXES:
            path = _miner_data_path(ip, suffix)
            with open(path, "w") as f:
                f.write("{}" if suffix.endswith(".json") else "")
            self._created_files.append(path)

    def _per_platform_files_present(self, mac, firmware=_FW):
        return [
            s
            for s in self.PER_PLATFORM_SUFFIXES
            if os.path.exists(_miner_platform_path(mac, firmware, s))
        ]

    def _legacy_files_present(self, ip):
        return [s for s in self.LEGACY_SUFFIXES if os.path.exists(_miner_data_path(ip, s))]

    def _log_present(self, mac):
        return os.path.exists(_miner_data_path(mac, ".log.jsonl"))

    # ─── /tuner/bulk/remove ──────────────────────────────────────────────

    def test_bulk_remove_canonical_shape(self):
        self._register(_MAC_A, "10.0.0.1")
        self._register(_MAC_B, "10.0.0.2", firmware="bixbit")
        self._stub_manager.engines[_MAC_A] = _FakeEngine(_MAC_A, "10.0.0.1")
        self._stub_manager.engines[_MAC_B] = _FakeEngine(_MAC_B, "10.0.0.2")
        self._seed_files_for(_MAC_A, "10.0.0.1", firmware="epic")
        self._seed_files_for(_MAC_B, "10.0.0.2", firmware="bixbit")

        status, body = self._request(
            "POST",
            "/tuner/bulk/remove",
            {"macs": [_MAC_A, _MAC_B]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("results", data)
        self.assertIn("summary", data)
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["succeeded"], 2)
        self.assertEqual(data["summary"]["failed"], 0)
        for mac in (_MAC_A, _MAC_B):
            self.assertTrue(data["results"][mac]["ok"])
            self.assertEqual(data["results"][mac]["detail"], {"removed": True})

        # Config + engine + files all wiped.
        self.assertEqual(state.CONFIG["fleet_ops"]["MINER_IPS"], [])
        self.assertNotIn(_MAC_A, state.MINER_CONFIGS)
        self.assertNotIn(_MAC_B, state.MINER_CONFIGS)
        self.assertEqual(self._stub_manager.popped, [_MAC_A, _MAC_B])
        self.assertEqual(self._stub_manager.engines, {})
        self.assertEqual(self._per_platform_files_present(_MAC_A, "epic"), [])
        self.assertEqual(self._per_platform_files_present(_MAC_B, "bixbit"), [])
        self.assertFalse(self._log_present(_MAC_A))
        self.assertFalse(self._log_present(_MAC_B))

    def test_bulk_remove_includes_log_jsonl(self):
        """Explicit assertion that the .log.jsonl tuner log is wiped — the
        completely-fresh-state guarantee the operator asked about."""
        self._register(_MAC_A, "10.0.0.5")
        self._seed_files_for(_MAC_A, "10.0.0.5")
        self.assertTrue(self._log_present(_MAC_A))

        status, body = self._request(
            "POST", "/tuner/bulk/remove", {"macs": [_MAC_A]}, cookie=self.cookie
        )
        self.assertEqual(status, 200)
        self.assertFalse(
            self._log_present(_MAC_A),
            "tuner log (.log.jsonl) must be deleted by bulk remove",
        )

    def test_bulk_remove_destroys_running_engine(self):
        """Engine.destroy() + thread.join must run for engines that are alive."""
        self._register(_MAC_A, "10.0.0.7")
        eng = _FakeEngine(_MAC_A, "10.0.0.7", alive=True)
        self._stub_manager.engines[_MAC_A] = eng

        status, _ = self._request(
            "POST", "/tuner/bulk/remove", {"macs": [_MAC_A]}, cookie=self.cookie
        )
        self.assertEqual(status, 200)
        self.assertTrue(eng._destroyed)
        self.assertTrue(eng.thread.joined)

    def test_bulk_remove_partial_failure(self):
        """One bad MAC surfaces as ok=false; the others still removed."""
        self._register(_MAC_A, "10.0.0.1")
        self._register(_MAC_B, "10.0.0.2")
        self._stub_manager.engines[_MAC_A] = _FakeEngine(_MAC_A, "10.0.0.1")
        # Engine for B raises during destroy()
        self._stub_manager.engines[_MAC_B] = _FakeEngine(
            _MAC_B, "10.0.0.2", alive=True, destroy_raises=RuntimeError("boom")
        )
        self._seed_files_for(_MAC_A, "10.0.0.1")
        self._seed_files_for(_MAC_B, "10.0.0.2")

        status, body = self._request(
            "POST",
            "/tuner/bulk/remove",
            {"macs": [_MAC_A, _MAC_B]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["summary"]["succeeded"], 1)
        self.assertEqual(data["summary"]["failed"], 1)
        self.assertTrue(data["results"][_MAC_A]["ok"])
        self.assertFalse(data["results"][_MAC_B]["ok"])
        self.assertIn("boom", data["results"][_MAC_B]["error"])
        # A fully wiped; B was already popped from MINER_IPS before the
        # destroy() call raised — that's documented behavior.
        self.assertNotIn("10.0.0.1", state.CONFIG["fleet_ops"]["MINER_IPS"])
        self.assertEqual(self._per_platform_files_present(_MAC_A, "epic"), [])

    def test_bulk_remove_empty_macs(self):
        status, body = self._request("POST", "/tuner/bulk/remove", {"macs": []}, cookie=self.cookie)
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["summary"], {"total": 0, "succeeded": 0, "failed": 0})
        self.assertEqual(data["results"], {})

    def test_bulk_remove_unknown_mac_succeeds_idempotently(self):
        """Unknown MACs are a no-op (no engine to destroy, no files to delete)
        but still report ok=true so a stale frontend selection doesn't hard-fail
        the batch."""
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []
        status, body = self._request(
            "POST",
            "/tuner/bulk/remove",
            {"macs": [_MAC_UNKNOWN]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["results"][_MAC_UNKNOWN]["ok"])
        self.assertEqual(data["summary"]["succeeded"], 1)

    def test_bulk_remove_legacy_ips_body_returns_400(self):
        """Hard cutover: the old {ips:...} body shape is rejected with HTTP 400."""
        status, body = self._request(
            "POST",
            "/tuner/bulk/remove",
            {"ips": ["10.0.0.1"]},
            cookie=self.cookie,
        )
        self.assertEqual(status, 400)

    def test_bulk_remove_requires_auth(self):
        status, _ = self._request("POST", "/tuner/bulk/remove", {"macs": [_MAC_A]}, cookie=None)
        self.assertEqual(status, 401)

    # ─── /tuner/miners/remove (regression after refactor) ────────────────

    def test_single_remove_still_wipes_all_per_platform_files(self):
        self._register(_MAC_A, "10.0.0.42")
        self._stub_manager.engines[_MAC_A] = _FakeEngine(_MAC_A, "10.0.0.42")
        self._seed_files_for(_MAC_A, "10.0.0.42")
        self.assertEqual(len(self._per_platform_files_present(_MAC_A, "epic")), 3)
        self.assertTrue(self._log_present(_MAC_A))

        status, body = self._request(
            "POST",
            "/tuner/miners/remove",
            {"mac": _MAC_A},
            cookie=self.cookie,
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["miners"], [])
        self.assertNotIn(_MAC_A, state.MINER_CONFIGS)
        self.assertEqual(self._per_platform_files_present(_MAC_A, "epic"), [])
        self.assertFalse(self._log_present(_MAC_A))


if __name__ == "__main__":
    unittest.main()
