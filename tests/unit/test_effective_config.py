"""Unit tests for EffectiveConfig wrapper.

Covers:
- Override takes precedence over platform-default CONFIG value
- Falls through to platform-default when no override
- Per-IP isolation: another IP's override doesn't leak
- __contains__ checks both override map and CONFIG
- get() returns default when key absent
- Mutations to MINER_CONFIGS are visible on the next read (no caching)
- __setitem__ is not implemented (read-only contract)
- config_lock IS acquired on every read path (__getitem__, __contains__, .get)
- Three-step resolution: per-miner override → per-platform default → fleet_ops
- Bixbit miner resolves from bixbit platform bucket
- Unknown key raises KeyError
"""

from __future__ import annotations

import unittest

from tuner_app import state
from tuner_app.config.effective import EffectiveConfig

_TEST_KEYS = {"SOME_KEY", "ANOTHER_KEY", "NOT_A_KEY", "KEY"}


class _CountingLock:
    """Wraps a real lock, counts acquire() calls; delegates context-manager protocol.

    Used by the lock-discipline tests below — Python's `_thread.lock`'s `acquire`
    method is read-only so it can't be patched directly. Replace the whole lock
    object with a wrapper instead.
    """

    def __init__(self, real_lock):
        self._real = real_lock
        self.acquire_count = 0

    def acquire(self, *args, **kwargs):
        self.acquire_count += 1
        return self._real.acquire(*args, **kwargs)

    def release(self):
        return self._real.release()

    def __enter__(self):
        self.acquire_count += 1
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def _set_platform_key(key, val, platform="epic"):
    state.CONFIG["defaults"][platform][key] = val


def _clear_test_keys():
    for platform in ("epic", "bixbit", "luxos", "braiins"):
        for k in _TEST_KEYS:
            state.CONFIG["defaults"][platform].pop(k, None)
    for k in _TEST_KEYS:
        state.CONFIG["fleet_ops"].pop(k, None)


class TestEffectiveConfig(unittest.TestCase):
    def setUp(self) -> None:
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self) -> None:
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_falls_through_to_default(self):
        """Test that config falls through to platform default when no override."""
        _set_platform_key("SOME_KEY", 100)
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config["SOME_KEY"], 100)

    def test_override_takes_precedence(self):
        """Test that override takes precedence over default."""
        _set_platform_key("SOME_KEY", 100)
        state.MINER_CONFIGS["1.2.3.4"] = {"SOME_KEY": 999, "firmware_type": "epic"}
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config["SOME_KEY"], 999)

    def test_other_ip_unaffected(self):
        """Test that other IPs are unaffected by overrides."""
        _set_platform_key("SOME_KEY", 100)
        state.MINER_CONFIGS["1.2.3.4"] = {"SOME_KEY": 999, "firmware_type": "epic"}
        config = EffectiveConfig("5.6.7.8")
        self.assertEqual(config["SOME_KEY"], 100)

    def test_contains_falls_through(self):
        """Test that 'in' operator falls through to default."""
        _set_platform_key("ANOTHER_KEY", 1)
        config = EffectiveConfig("1.2.3.4")
        self.assertTrue("ANOTHER_KEY" in config)
        self.assertFalse("NOT_A_KEY" in config)

    def test_get_with_default(self):
        """Test that get() works with default values."""
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config.get("NOT_A_KEY", 42), 42)
        _set_platform_key("SOME_KEY", 55)
        self.assertEqual(config.get("SOME_KEY"), 55)

    def test_setting_override_visible_immediately(self):
        """Test that setting an override is immediately visible (no caching)."""
        _set_platform_key("KEY", 100)
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config["KEY"], 100)
        state.MINER_CONFIGS["1.2.3.4"] = {"KEY": 555, "firmware_type": "epic"}
        self.assertEqual(config["KEY"], 555)

    def test_no_setitem(self):
        """Test that setting items raises an error (read-only contract)."""
        config = EffectiveConfig("1.2.3.4")
        with self.assertRaises((TypeError, AttributeError)):
            config["SOME_KEY"] = 99

    def test_config_lock_is_acquired_on_read(self):
        """Test that config lock is acquired on __getitem__ read."""
        _set_platform_key("SOME_KEY", 100)
        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter
        try:
            config = EffectiveConfig("1.2.3.4")
            _ = config["SOME_KEY"]
            self.assertGreaterEqual(counter.acquire_count, 1)
        finally:
            state.config_lock = original

    def test_config_lock_acquired_on_contains(self):
        """Test that config lock is acquired on 'in' check."""
        _set_platform_key("KEY", 1)
        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter
        try:
            config = EffectiveConfig("1.2.3.4")
            _ = "KEY" in config
            self.assertGreaterEqual(counter.acquire_count, 1)
        finally:
            state.config_lock = original

    def test_config_lock_acquired_on_get(self):
        """Test that config lock is acquired on get()."""
        _set_platform_key("KEY", 1)
        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter
        try:
            config = EffectiveConfig("1.2.3.4")
            config.get("KEY")
            self.assertGreaterEqual(counter.acquire_count, 1)
        finally:
            state.config_lock = original

    # ── new v3 three-step resolution tests ───────────────────────────────────

    def test_three_step_per_miner_override_wins(self):
        """Per-miner override beats platform default and fleet_ops."""
        _set_platform_key("SOME_KEY", 10)
        state.CONFIG["fleet_ops"]["SOME_KEY"] = 20
        state.MINER_CONFIGS["1.2.3.4"] = {"SOME_KEY": 99, "firmware_type": "epic"}
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config["SOME_KEY"], 99)

    def test_falls_through_to_platform_default_for_known_platform_key(self):
        """No override → platform bucket value is returned."""
        _set_platform_key("SOME_KEY", 42, platform="epic")
        state.MINER_CONFIGS["1.2.3.4"] = {"firmware_type": "epic"}
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config["SOME_KEY"], 42)

    def test_falls_through_to_fleet_ops_for_singleton_key(self):
        """Fleet-only key not in platform bucket → fleet_ops value returned."""
        state.CONFIG["fleet_ops"]["SOME_KEY"] = 77
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config["SOME_KEY"], 77)

    def test_unknown_key_raises_KeyError(self):
        """Key absent from overrides, platform bucket, and fleet_ops → KeyError."""
        config = EffectiveConfig("1.2.3.4")
        with self.assertRaises(KeyError):
            _ = config["NOT_A_KEY"]

    def test_bixbit_miner_resolves_from_bixbit_bucket(self):
        """Bixbit miner resolves SOME_KEY from the bixbit platform bucket."""
        _set_platform_key("SOME_KEY", 55, platform="epic")
        _set_platform_key("SOME_KEY", 88, platform="bixbit")
        state.MINER_CONFIGS["1.2.3.4"] = {"firmware_type": "bixbit"}
        config = EffectiveConfig("1.2.3.4")
        self.assertEqual(config["SOME_KEY"], 88)

    def test_lock_acquired_exactly_once_per_getitem(self):
        """__getitem__ acquires config_lock exactly once (single lock acquisition)."""
        _set_platform_key("SOME_KEY", 100)
        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter
        try:
            config = EffectiveConfig("1.2.3.4")
            count_before = counter.acquire_count
            _ = config["SOME_KEY"]
            self.assertEqual(
                counter.acquire_count - count_before,
                1,
                "__getitem__ must acquire config_lock exactly once",
            )
        finally:
            state.config_lock = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
