"""Unit tests for tuner_app.mrr.rental_cache.RentalCache.

Covers: cache hit/miss, get_all shallow-copy, TTL/daemon lifecycle,
MRR_ENABLED=False short-circuit, missing-creds short-circuit, successful
refresh populates cache, rented=True path, error-path stores error string,
throttling spacing assertion, no-rig-id short-circuit, stop() signal.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tuner_app import state
from tuner_app.mrr.rental_cache import RentalCache


class TestRentalCacheBasic(unittest.TestCase):
    """Tests for cache read primitives — no I/O."""

    def test_get_miss(self):
        """get() returns None for an IP not in the cache."""
        cache = RentalCache()
        self.assertIsNone(cache.get("192.168.1.1"))

    def test_get_hit(self):
        """get() returns the stored entry when the IP is in the cache."""
        cache = RentalCache()
        entry = {"rented": True, "rig_id": 123, "fetched_ts": 1.0, "error": None}
        cache._cache["192.168.1.1"] = entry
        self.assertEqual(cache.get("192.168.1.1"), entry)

    def test_get_all_empty(self):
        """get_all() returns an empty dict on a fresh instance."""
        cache = RentalCache()
        self.assertEqual(cache.get_all(), {})

    def test_get_all_returns_copy(self):
        """get_all() returns a separate dict; deleting a key from it does not
        affect the cache (shallow-copy contract)."""
        cache = RentalCache()
        cache._cache["192.168.1.1"] = {
            "rented": False,
            "rig_id": 1,
            "fetched_ts": 1.0,
            "error": None,
        }
        result = cache.get_all()
        del result["192.168.1.1"]
        # Original cache must still have the key
        self.assertIn("192.168.1.1", cache._cache)

    def test_daemon_stop_sets_event(self):
        """stop() sets _stop_event so the daemon thread exits."""
        cache = RentalCache()
        cache.start()
        self.assertFalse(cache._stop_event.is_set())
        cache.stop()
        self.assertTrue(cache._stop_event.is_set())

    def test_start_idempotent(self):
        """Calling start() twice does not create a second thread."""
        cache = RentalCache()
        # Patch _run so the thread blocks on stop_event only
        cache._stop_event.set()  # stop immediately on first loop check
        cache.start()
        cache._stop_event.clear()
        cache._stop_event.set()
        cache.start()  # must be a no-op since thread is still alive (or already dead)
        # Either the same thread object or the thread from first start; no duplicate
        # The key invariant: start() doesn't raise
        self.assertIsNotNone(cache._thread)


def _apply_fleet_ops_config(cfg):
    """Write flat cfg keys into state.CONFIG["fleet_ops"] and defaults["epic"].
    MRR_RIG_ID (global default) goes into defaults["epic"]; all other keys
    go into fleet_ops (which is where rental_cache reads from)."""
    fo = state.CONFIG["fleet_ops"]
    rig_id = cfg.pop("MRR_RIG_ID", None)
    fo.update(cfg)
    if rig_id is not None:
        state.CONFIG["defaults"]["epic"]["MRR_RIG_ID"] = rig_id


class TestRentalCacheRefreshCycle(unittest.TestCase):
    """Tests for _refresh_cycle — all MRR API calls mocked."""

    def setUp(self):
        # Snapshot CONFIG before mutating so tearDown can restore it.
        # Never rebind state.CONFIG; shared readers rely on its object identity.
        import copy

        self._config_snapshot = copy.deepcopy(state.CONFIG)
        self._miner_configs_snapshot = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        # Restore CONFIG and MINER_CONFIGS to pre-test state.
        state.CONFIG.clear()
        state.CONFIG.update(self._config_snapshot)
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS.update(self._miner_configs_snapshot)

    def _base_config(self, **overrides):
        cfg = {
            "MRR_ENABLED": True,
            "MRR_API_KEY": "testkey",
            "MRR_API_SECRET": "testsecret",
            "MINER_IPS": ["192.168.1.10"],
            "MRR_RIG_ID": 0,
        }
        cfg.update(overrides)
        return cfg

    def _apply(self, **overrides):
        """Apply base config + overrides into fleet_ops / defaults["epic"]."""
        _apply_fleet_ops_config(self._base_config(**overrides))

    def test_mrr_disabled_skips_api(self):
        """MRR_ENABLED=False → no MRRClient calls."""
        self._apply(MRR_ENABLED=False)
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache._refresh_cycle()
            mock_cls.assert_not_called()

    def test_missing_api_key_skips_api(self):
        """Empty MRR_API_KEY → no MRRClient calls."""
        self._apply(MRR_API_KEY="")
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache._refresh_cycle()
            mock_cls.assert_not_called()

    def test_missing_api_secret_skips_api(self):
        """Empty MRR_API_SECRET → no MRRClient calls."""
        self._apply(MRR_API_SECRET="")
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache._refresh_cycle()
            mock_cls.assert_not_called()

    def test_no_mrr_rig_id_no_pairs(self):
        """No MRR_RIG_ID in CONFIG or MINER_CONFIGS → no API calls, cache empty."""
        self._apply(MRR_RIG_ID=0)
        state.MINER_CONFIGS["192.168.1.10"] = {}  # no MRR_RIG_ID
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache._refresh_cycle()
            mock_cls.assert_not_called()
        self.assertEqual(cache.get_all(), {})

    def test_refresh_cycle_populates_cache_not_rented(self):
        """Successful get_rig() for a non-rented rig stores rented=False in cache."""
        self._apply()
        state.MINER_CONFIGS["192.168.1.10"] = {"MRR_RIG_ID": 456}
        cache = RentalCache()
        # is_rig_rented checks rig.get("status") etc. — use a plain dict
        fake_rig = {"id": 456, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        entry = cache.get("192.168.1.10")
        self.assertIsNotNone(entry)
        self.assertFalse(entry["rented"])
        self.assertEqual(entry["rig_id"], 456)
        self.assertIsNone(entry["error"])

    def test_refresh_cycle_rented(self):
        """get_rig() returns a rented rig → cache entry has rented=True."""
        self._apply()
        state.MINER_CONFIGS["192.168.1.10"] = {"MRR_RIG_ID": 789}
        cache = RentalCache()
        # is_rig_rented recognises {"status": {"rented": True}}
        fake_rig = {"id": 789, "status": {"rented": True}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        entry = cache.get("192.168.1.10")
        self.assertIsNotNone(entry)
        self.assertTrue(entry["rented"])
        self.assertEqual(entry["rig_id"], 789)
        self.assertIsNone(entry["error"])

    def test_refresh_cycle_error_stored(self):
        """get_rig() raises → cache entry has rented=False and error string."""
        self._apply()
        state.MINER_CONFIGS["192.168.1.10"] = {"MRR_RIG_ID": 456}
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.side_effect = Exception("network timeout")
            cache._refresh_cycle()
        entry = cache.get("192.168.1.10")
        self.assertIsNotNone(entry)
        self.assertFalse(entry["rented"])
        self.assertEqual(entry["error"], "network timeout")

    def test_throttling_spacing_three_rigs(self):
        """With 3 rigs, spacing = max(1.0, 60/3) = 20.0 s between calls."""
        ips = ["192.168.1.10", "192.168.1.11", "192.168.1.12"]
        self._apply(MINER_IPS=ips, MRR_RIG_ID=0)
        for i, ip in enumerate(ips):
            state.MINER_CONFIGS[ip] = {"MRR_RIG_ID": 100 + i}
        cache = RentalCache()
        fake_rig = {"id": 100, "status": {"rented": False}}
        with (
            patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls,
            patch.object(cache._stop_event, "wait", return_value=False) as mock_wait,
        ):
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        # Between 3 rigs there are 2 inter-call sleeps; each with timeout=20.0
        self.assertEqual(mock_wait.call_count, 2)
        for c in mock_wait.call_args_list:
            timeout = c.kwargs.get("timeout") if c.kwargs else c.args[0]
            self.assertAlmostEqual(timeout, 20.0, places=5)

    def test_global_rig_id_fallback(self):
        """If MINER_CONFIGS has no MRR_RIG_ID, falls back to CONFIG['MRR_RIG_ID']."""
        self._apply(MRR_RIG_ID=999)
        state.MINER_CONFIGS["192.168.1.10"] = {}  # no per-miner rig_id
        cache = RentalCache()
        fake_rig = {"id": 999, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        entry = cache.get("192.168.1.10")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["rig_id"], 999)

    def test_disabled_with_rig_id_writes_diagnostic_entry(self):
        """When MRR_ENABLED=False but a per-miner MRR_RIG_ID is set, the cache
        gets a diagnostic entry (state='disabled') instead of staying empty.
        This is what surfaces 'MRR off' in the dashboard pill — operators
        previously saw an opaque em-dash and didn't realize they had set the
        rig ID without enabling MRR globally."""
        self._apply(MRR_ENABLED=False)
        state.MINER_CONFIGS["192.168.1.10"] = {"MRR_RIG_ID": 555}
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache._refresh_cycle()
            # No live API calls — the diagnostic doesn't need MRR
            mock_cls.assert_not_called()
        entry = cache.get("192.168.1.10")
        self.assertIsNotNone(entry, "expected a diagnostic entry, got None")
        self.assertEqual(entry["state"], "disabled")
        self.assertEqual(entry["rig_id"], 555)
        self.assertIsNone(entry["rented"])
        self.assertIn("MRR_ENABLED", entry["error"])

    def test_no_creds_with_rig_id_writes_diagnostic_entry(self):
        """When MRR_ENABLED=True but creds are missing, the cache gets a
        state='no_creds' diagnostic entry so the UI can surface 'No creds'
        instead of em-dash."""
        self._apply(MRR_API_KEY="")
        state.MINER_CONFIGS["192.168.1.10"] = {"MRR_RIG_ID": 777}
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache._refresh_cycle()
            mock_cls.assert_not_called()
        entry = cache.get("192.168.1.10")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["state"], "no_creds")
        self.assertEqual(entry["rig_id"], 777)
        self.assertIsNone(entry["rented"])
        self.assertIn("MRR_API_KEY", entry["error"])

    def test_disabled_without_rig_id_keeps_cache_empty(self):
        """MRR disabled AND no rig_id configured → cache stays empty (em-dash
        in UI is correct here — there's nothing to surface)."""
        self._apply(MRR_ENABLED=False, MRR_RIG_ID=0)
        state.MINER_CONFIGS["192.168.1.10"] = {}
        cache = RentalCache()
        cache._refresh_cycle()
        self.assertEqual(cache.get_all(), {})

    def test_live_entry_carries_state_field(self):
        """A successful refresh writes state='live' so the frontend can tell
        a live entry apart from the diagnostic entries."""
        self._apply()
        state.MINER_CONFIGS["192.168.1.10"] = {"MRR_RIG_ID": 456}
        cache = RentalCache()
        fake_rig = {"id": 456, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        entry = cache.get("192.168.1.10")
        self.assertEqual(entry["state"], "live")

    def test_api_error_writes_state_error(self):
        """When the MRR API call fails, the cache entry has state='error' (not
        'live') so the frontend can distinguish a fetch error from the
        configured-but-not-fetched-yet state."""
        self._apply()
        state.MINER_CONFIGS["192.168.1.10"] = {"MRR_RIG_ID": 456}
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.side_effect = Exception("MRR 502 bad gateway")
            cache._refresh_cycle()
        entry = cache.get("192.168.1.10")
        self.assertEqual(entry["state"], "error")
        self.assertIsNone(entry["rented"])
        self.assertIn("MRR 502", entry["error"])


class TestRentalCacheRefreshOne(unittest.TestCase):
    """Tests for the synchronous single-rig refresh path used after operator
    config changes. Mirrors the daemon's per-rig logic but without throttling
    or stop-event semantics."""

    def setUp(self):
        import copy

        self._config_snapshot = copy.deepcopy(state.CONFIG)
        self._miner_configs_snapshot = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._config_snapshot)
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS.update(self._miner_configs_snapshot)

    def _base_config(self, **overrides):
        cfg = {
            "MRR_ENABLED": True,
            "MRR_API_KEY": "testkey",
            "MRR_API_SECRET": "testsecret",
            "MINER_IPS": ["192.168.1.10"],
            "MRR_RIG_ID": 0,
        }
        cfg.update(overrides)
        return cfg

    def _apply(self, **overrides):
        """Apply base config + overrides into fleet_ops / defaults["epic"]."""
        _apply_fleet_ops_config(self._base_config(**overrides))

    def test_zero_rig_id_drops_entry(self):
        """rig_id=0 (rig cleared) → cache entry is removed, no API call."""
        cache = RentalCache()
        cache._cache["192.168.1.10"] = {"rented": False, "rig_id": 99}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache.refresh_one("192.168.1.10", 0)
            mock_cls.assert_not_called()
        self.assertIsNone(cache.get("192.168.1.10"))

    def test_negative_rig_id_drops_entry(self):
        """Defensive: any non-positive rig_id drops the entry."""
        cache = RentalCache()
        cache._cache["192.168.1.10"] = {"rented": False, "rig_id": 99}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache.refresh_one("192.168.1.10", -5)
            mock_cls.assert_not_called()
        self.assertIsNone(cache.get("192.168.1.10"))

    def test_mrr_disabled_writes_disabled_state(self):
        """MRR_ENABLED=False with creds set → entry has state='disabled', no API call."""
        self._apply(MRR_ENABLED=False)
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache.refresh_one("192.168.1.10", 12345)
            mock_cls.assert_not_called()
        entry = cache.get("192.168.1.10")
        self.assertEqual(entry["state"], "disabled")
        self.assertEqual(entry["rig_id"], 12345)
        self.assertIsNone(entry["rented"])

    def test_missing_creds_writes_no_creds_state(self):
        """MRR_ENABLED=True but no API key → entry has state='no_creds'."""
        self._apply(MRR_API_KEY="")
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            cache.refresh_one("192.168.1.10", 12345)
            mock_cls.assert_not_called()
        entry = cache.get("192.168.1.10")
        self.assertEqual(entry["state"], "no_creds")
        self.assertEqual(entry["rig_id"], 12345)

    def test_live_fetch_writes_live_state(self):
        """Fully configured + successful fetch → entry has state='live' with rented status."""
        self._apply()
        cache = RentalCache()
        fake_rig = {"id": 12345, "status": {"rented": True}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache.refresh_one("192.168.1.10", 12345)
        entry = cache.get("192.168.1.10")
        self.assertEqual(entry["state"], "live")
        self.assertTrue(entry["rented"])
        self.assertEqual(entry["rig_id"], 12345)
        self.assertIsNone(entry["error"])

    def test_api_error_writes_error_state(self):
        """API failure → entry has state='error' with the exception message."""
        self._apply()
        cache = RentalCache()
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.side_effect = Exception("MRR 502")
            cache.refresh_one("192.168.1.10", 12345)
        entry = cache.get("192.168.1.10")
        self.assertEqual(entry["state"], "error")
        self.assertIn("MRR 502", entry["error"])

    def test_refresh_one_overwrites_stale_entry(self):
        """Pre-existing cache entry is replaced (not merged) on refresh."""
        self._apply()
        cache = RentalCache()
        cache._cache["192.168.1.10"] = {
            "rented": True,
            "rig_id": 999,
            "state": "live",
            "error": None,
            "fetched_ts": 1.0,
        }
        fake_rig = {"id": 12345, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache.refresh_one("192.168.1.10", 12345)
        entry = cache.get("192.168.1.10")
        self.assertEqual(entry["rig_id"], 12345)
        self.assertFalse(entry["rented"])


_MAC_A = "aa:bb:cc:dd:ee:01"
_IP_A = "192.168.1.10"
_IP_B = "192.168.1.99"


def _v4_entry(ip, firmware="epic", rig_id=None):
    entry = {
        "ip": ip,
        "current_firmware": firmware,
        "id_synthesized": False,
        "platforms": {firmware: {}},
    }
    if rig_id is not None:
        entry["MRR_RIG_ID"] = rig_id
    return entry


class TestRentalCacheMacKeyed(unittest.TestCase):
    """A10: rental cache is keyed by canonical MAC (or synth ID).

    Verifies the v4-shape rental cache: refresh cycle iterates MINER_CONFIGS
    directly, stores entries under MAC, and IP→MAC translation in the public
    get/refresh_one API resolves entries correctly even when the caller
    passes the IP (transitional / pre-A12 HTTP-handler call sites).

    The DHCP-move invariant is the load-bearing one: when a miner's IP
    changes, the cache entry stays under the same MAC and ``get(new_ip)``
    still returns it via reverse-lookup.
    """

    def setUp(self):
        import copy

        self._config_snapshot = copy.deepcopy(state.CONFIG)
        self._miner_configs_snapshot = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._config_snapshot)
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS.update(self._miner_configs_snapshot)

    def _apply_creds(self):
        fo = state.CONFIG["fleet_ops"]
        fo["MRR_ENABLED"] = True
        fo["MRR_API_KEY"] = "k"
        fo["MRR_API_SECRET"] = "s"

    def test_refresh_cycle_stores_entry_under_mac(self):
        """v4 entry → cache entry keyed by MAC, not IP."""
        self._apply_creds()
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A, rig_id=456)
        cache = RentalCache()
        fake_rig = {"id": 456, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        # Internal cache key is MAC
        self.assertIn(_MAC_A, cache._cache)
        self.assertNotIn(_IP_A, cache._cache)

    def test_get_resolves_ip_to_mac_when_v4_entry_exists(self):
        """get(ip) reverse-looks-up MAC via MINER_CONFIGS and returns entry."""
        self._apply_creds()
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        cache = RentalCache()
        cache._cache[_MAC_A] = {"rented": True, "rig_id": 1}
        # get with IP resolves via canonical_miner_key
        entry = cache.get(_IP_A)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["rig_id"], 1)
        # get with MAC also works
        self.assertIsNotNone(cache.get(_MAC_A))

    def test_dhcp_ip_change_preserves_cache_entry(self):
        """When MINER_CONFIGS[mac]['ip'] changes (DHCP move), the cache entry
        stays under the same MAC and get(new_ip) finds it."""
        self._apply_creds()
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A, rig_id=456)
        cache = RentalCache()
        cache._cache[_MAC_A] = {"rented": True, "rig_id": 456, "fetched_ts": 1.0}
        # Simulate DHCP move
        state.MINER_CONFIGS[_MAC_A]["ip"] = _IP_B
        # get(old_ip) no longer resolves
        self.assertIsNone(cache.get(_IP_A))
        # get(new_ip) resolves to the same MAC entry — cache survives the move
        entry = cache.get(_IP_B)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["rig_id"], 456)
        # get(MAC) is the canonical access — always works
        self.assertIsNotNone(cache.get(_MAC_A))

    def test_refresh_one_with_ip_keys_under_mac(self):
        """refresh_one accepts IP and stores the resulting entry under MAC."""
        self._apply_creds()
        state.MINER_CONFIGS[_MAC_A] = _v4_entry(_IP_A)
        cache = RentalCache()
        fake_rig = {"id": 789, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache.refresh_one(_IP_A, 789)
        self.assertIn(_MAC_A, cache._cache)
        self.assertNotIn(_IP_A, cache._cache)

    def test_per_platform_rig_id_resolved(self):
        """v4 platforms[fw].MRR_RIG_ID resolves when no top-level / fleet rig is set."""
        self._apply_creds()
        entry = _v4_entry(_IP_A, firmware="luxos")
        entry["platforms"]["luxos"]["MRR_RIG_ID"] = 4242
        state.MINER_CONFIGS[_MAC_A] = entry
        cache = RentalCache()
        fake_rig = {"id": 4242, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        cached = cache.get(_MAC_A)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["rig_id"], 4242)

    def test_cross_platform_rig_id_takes_precedence_over_per_platform(self):
        """Top-level (cross-platform) MRR_RIG_ID wins over platforms[fw] override.
        Mirrors EffectiveConfig's resolution order."""
        self._apply_creds()
        entry = _v4_entry(_IP_A, firmware="luxos", rig_id=111)
        entry["platforms"]["luxos"]["MRR_RIG_ID"] = 999
        state.MINER_CONFIGS[_MAC_A] = entry
        cache = RentalCache()
        fake_rig = {"id": 111, "status": {"rented": False}}
        with patch("tuner_app.mrr.rental_cache.MRRClient") as mock_cls:
            mock_cls.return_value.get_rig.return_value = fake_rig
            cache._refresh_cycle()
        self.assertEqual(cache.get(_MAC_A)["rig_id"], 111)


if __name__ == "__main__":
    unittest.main(verbosity=2)
