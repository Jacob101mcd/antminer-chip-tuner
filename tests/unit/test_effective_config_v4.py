"""Unit tests for EffectiveConfig v4-aware behavior (A6 spec).

Spec contract under test
------------------------
A6 extends EffectiveConfig to support MAC-keyed MINER_CONFIGS (v4 schema)
as a transitional adapter while IP-based callers continue to work.

Resolution order (4 steps):
  1. Cross-platform per-miner override: MINER_CONFIGS[key].get(k) when
     k is in CROSS_PLATFORM_PER_MINER_KEYS.
  2. Per-platform per-miner override: MINER_CONFIGS[key]["platforms"][fw][k]
     for v4-shape entries; flat MINER_CONFIGS[key][k] for v3-shape (legacy).
  3. Per-platform default: CONFIG["defaults"][fw][k].
  4. Fleet-ops singleton: CONFIG["fleet_ops"][k].
  5. KeyError.

Identifier detection:
- IPv4 pattern → reverse-lookup in MINER_CONFIGS by "ip" field (v4 path).
  If not found → fall back to direct dict key (v3/legacy path).
- Non-IPv4 (MAC, synth ID) → direct dict key (v4 path).

.ip property:
- v4-shape entry: MINER_CONFIGS[key].get("ip", "").
- v3-shape / IP-key fallback: returns the identifier itself.

Backward compatibility:
- Existing v3-pattern `MINER_CONFIGS["1.2.3.4"] = {"firmware_type": "epic",
  "SOME_KEY": 999}` keeps working via the IP fallback → legacy v3 path.

CROSS_PLATFORM_PER_MINER_KEYS = frozenset({"PASSWORD", "MRR_RIG_ID",
                                            "hostname", "current_firmware"})
"""

from __future__ import annotations

import unittest

from tuner_app import state
from tuner_app.config.effective import EffectiveConfig
from tuner_app.constants import CROSS_PLATFORM_PER_MINER_KEYS

# ---------------------------------------------------------------------------
# Test-key sentinel set — cleared in setUp/tearDown to avoid bleed
# ---------------------------------------------------------------------------

_TEST_KEYS = {
    "SOME_KEY",
    "ANOTHER_KEY",
    "NOT_A_KEY",
    "KEY",
    "VOLTAGE_MV",
    "CHIP_FREQ_SPREAD_MHZ",
    "RANDOM_KEY",
    "SPURIOUS_KEY",
    "NOT_WHITELISTED",
}


# ---------------------------------------------------------------------------
# _CountingLock — copied from test_effective_config.py (same pattern)
# ---------------------------------------------------------------------------


class _CountingLock:
    """Wraps a real lock; counts acquire() calls; delegates context-manager protocol.

    Python's ``_thread.lock.acquire`` is a read-only C slot — it cannot be
    monkey-patched directly.  Replace the entire lock object with a wrapper
    for the duration of each lock-discipline test.
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_default(key, val, platform="epic"):
    state.CONFIG["defaults"][platform][key] = val


def _clear_test_keys():
    for platform in ("epic", "bixbit", "luxos", "braiins"):
        for k in _TEST_KEYS:
            state.CONFIG["defaults"][platform].pop(k, None)
    for k in _TEST_KEYS:
        state.CONFIG["fleet_ops"].pop(k, None)


def _v4_entry(ip, firmware="epic", platforms=None, **top_level):
    """Build a minimal v4-shape MINER_CONFIGS entry."""
    entry = {
        "ip": ip,
        "current_firmware": firmware,
        "id_synthesized": False,
        "platforms": platforms if platforms is not None else {firmware: {}},
    }
    entry.update(top_level)
    return entry


# ---------------------------------------------------------------------------
# 1. TestEffectiveConfigDirectMacLookup
# ---------------------------------------------------------------------------


class TestEffectiveConfigDirectMacLookup(unittest.TestCase):
    """MAC or synth-ID identifiers are treated as direct dict keys into MINER_CONFIGS."""

    _MAC = "aa:bb:cc:dd:ee:01"
    _SYNTH = "syn-10-0-0-5-deadbeef"

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_mac_direct_lookup_reads_platform_default(self):
        """EffectiveConfig(mac) with a v4 entry falls through to CONFIG defaults[fw][KEY]."""
        _set_default("SOME_KEY", 42, platform="epic")
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["SOME_KEY"], 42)

    def test_mac_direct_lookup_reads_per_platform_override(self):
        """Per-platform override inside platforms[epic][KEY] wins over CONFIG default."""
        _set_default("VOLTAGE_MV", 14000, platform="epic")
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            "10.0.0.1",
            platforms={"epic": {"VOLTAGE_MV": 13500}},
        )
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["VOLTAGE_MV"], 13500)

    def test_mac_direct_lookup_reads_cross_platform_override(self):
        """PASSWORD at the top level (CROSS_PLATFORM_PER_MINER_KEYS) wins over fleet_ops.

        Also verifies the whitelist gate: a non-whitelisted key at the same top-level
        position must NOT be returned via step 1 (KeyError expected).
        """
        state.CONFIG["fleet_ops"]["PASSWORD"] = "fleet-pass"
        entry = _v4_entry("10.0.0.1", PASSWORD="miner-pass")
        # Add a non-whitelisted top-level key — must NOT be returned via step 1.
        entry["NOT_WHITELISTED"] = "leak"
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        # Positive: whitelisted key is returned
        self.assertEqual(cfg["PASSWORD"], "miner-pass")
        # Negative: non-whitelisted key at top level must NOT resolve via step 1
        with self.assertRaises(KeyError):
            _ = cfg["NOT_WHITELISTED"]

    def test_mac_direct_lookup_falls_through_to_fleet_ops(self):
        """Key absent from per-miner and defaults but present in fleet_ops → fleet_ops value."""
        state.CONFIG["fleet_ops"]["SOME_KEY"] = 77
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["SOME_KEY"], 77)

    def test_mac_direct_lookup_unknown_key_raises_KeyError(self):
        """Key absent from all sources → KeyError (not AttributeError or None)."""
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        with self.assertRaises(KeyError):
            _ = cfg["NOT_A_KEY"]

    def test_synth_id_lookup_works_like_mac(self):
        """EffectiveConfig(synth_id) resolves using the same direct-key v4 path."""
        _set_default("SOME_KEY", 55, platform="epic")
        state.MINER_CONFIGS[self._SYNTH] = _v4_entry("10.0.0.5")
        cfg = EffectiveConfig(self._SYNTH)
        self.assertEqual(cfg["SOME_KEY"], 55)

    def test_mac_mrr_rig_id_cross_platform_override(self):
        """MRR_RIG_ID at the top level resolves as a cross-platform key.

        Also verifies the whitelist gate: a non-whitelisted key at the same top-level
        position must NOT be returned via step 1 (KeyError expected).
        """
        entry = _v4_entry("10.0.0.1", MRR_RIG_ID=12345)
        # Add a non-whitelisted top-level key — must NOT be returned via step 1.
        entry["NOT_WHITELISTED"] = "should-not-leak"
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        # Positive: whitelisted key is returned
        self.assertEqual(cfg["MRR_RIG_ID"], 12345)
        # Negative: non-whitelisted key at top level must NOT resolve via step 1
        with self.assertRaises(KeyError):
            _ = cfg["NOT_WHITELISTED"]

    def test_mac_hostname_cross_platform_override(self):
        """hostname at the top level resolves as a cross-platform key.

        Also verifies the whitelist gate: a non-whitelisted key at the same top-level
        position must NOT be returned via step 1 (KeyError expected).
        """
        entry = _v4_entry("10.0.0.1", hostname="miner-host")
        # Add a non-whitelisted top-level key — must NOT be returned via step 1.
        entry["NOT_WHITELISTED"] = "should-not-leak"
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        # Positive: whitelisted key is returned
        self.assertEqual(cfg["hostname"], "miner-host")
        # Negative: non-whitelisted key at top level must NOT resolve via step 1
        with self.assertRaises(KeyError):
            _ = cfg["NOT_WHITELISTED"]


# ---------------------------------------------------------------------------
# 2. TestEffectiveConfigIPReverseLookup
# ---------------------------------------------------------------------------


class TestEffectiveConfigIPReverseLookup(unittest.TestCase):
    """IP identifiers trigger reverse-lookup; fall back to legacy v3 if not found."""

    _MAC = "aa:bb:cc:dd:ee:02"
    _IP = "10.0.0.5"
    _LEGACY_IP = "1.2.3.4"

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_ip_reverse_lookup_finds_v4_entry(self):
        """EffectiveConfig(ip) finds the MAC-keyed entry whose ip field equals the IP."""
        _set_default("VOLTAGE_MV", 13500, platform="luxos")
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            self._IP,
            firmware="luxos",
            platforms={"luxos": {"VOLTAGE_MV": 13000}},
        )
        cfg = EffectiveConfig(self._IP)
        # Per-platform override in the v4 entry must be returned
        self.assertEqual(cfg["VOLTAGE_MV"], 13000)

    def test_ip_reverse_lookup_falls_back_to_legacy_v3_when_not_found(self):
        """When no v4 entry has ip==identifier, falls back to direct v3 key lookup."""
        _set_default("SOME_KEY", 10)
        # Inject a v3-shape entry directly under the IP key — no reverse match possible
        state.MINER_CONFIGS[self._LEGACY_IP] = {"firmware_type": "epic", "SOME_KEY": 99}
        cfg = EffectiveConfig(self._LEGACY_IP)
        # Legacy path: per-miner flat override wins
        self.assertEqual(cfg["SOME_KEY"], 99)

    def test_ip_reverse_lookup_legacy_v3_reads_per_miner_override(self):
        """Legacy v3 flat per-miner key SOME_KEY=999 is returned by __getitem__."""
        state.MINER_CONFIGS[self._LEGACY_IP] = {"firmware_type": "epic", "SOME_KEY": 999}
        cfg = EffectiveConfig(self._LEGACY_IP)
        self.assertEqual(cfg["SOME_KEY"], 999)

    def test_ip_reverse_lookup_legacy_v3_falls_through_to_default(self):
        """Legacy v3 entry with no per-miner key falls through to platform default."""
        _set_default("ANOTHER_KEY", 123)
        state.MINER_CONFIGS[self._LEGACY_IP] = {"firmware_type": "epic"}
        cfg = EffectiveConfig(self._LEGACY_IP)
        self.assertEqual(cfg["ANOTHER_KEY"], 123)

    def test_ip_reverse_lookup_prefers_v4_over_legacy_when_both_exist(self):
        """When v4 entry has ip==identifier, it wins over IP-keyed v3 entry for the same IP."""
        _set_default("SOME_KEY", 10)
        # Both entries injected: one v4 MAC-keyed with ip field, one v3 IP-keyed
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            self._IP,
            platforms={"epic": {"SOME_KEY": 200}},
        )
        state.MINER_CONFIGS[self._IP] = {"firmware_type": "epic", "SOME_KEY": 300}
        cfg = EffectiveConfig(self._IP)
        # Reverse-lookup finds the v4 MAC entry first
        self.assertEqual(cfg["SOME_KEY"], 200)


# ---------------------------------------------------------------------------
# 3. TestEffectiveConfigIpProperty
# ---------------------------------------------------------------------------


class TestEffectiveConfigIpProperty(unittest.TestCase):
    """The .ip property returns the miner IP regardless of identifier type."""

    _MAC = "aa:bb:cc:dd:ee:03"
    _LEGACY_IP = "1.2.3.4"

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_ip_property_v4_returns_entry_ip_field(self):
        """EffectiveConfig(mac).ip returns MINER_CONFIGS[mac]['ip']."""
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.5")
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg.ip, "10.0.0.5")

    def test_ip_property_legacy_v3_returns_identifier(self):
        """EffectiveConfig(ip).ip returns the identifier itself for a legacy v3 entry."""
        state.MINER_CONFIGS[self._LEGACY_IP] = {"firmware_type": "epic"}
        cfg = EffectiveConfig(self._LEGACY_IP)
        self.assertEqual(cfg.ip, self._LEGACY_IP)

    def test_ip_property_v4_returns_empty_string_when_no_ip_field(self):
        """v4-shape entry without 'ip' field returns '' (defensive default, never raises)."""
        entry = {
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
            # no 'ip' key
        }
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg.ip, "")

    def test_ip_property_acquires_config_lock(self):
        """EffectiveConfig.ip read is protected by state.config_lock."""
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.5")
        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter
        try:
            cfg = EffectiveConfig(self._MAC)
            count_before = counter.acquire_count
            _ = cfg.ip
            self.assertGreater(counter.acquire_count, count_before)
        finally:
            state.config_lock = original


# ---------------------------------------------------------------------------
# 4. TestEffectiveConfigFourStepResolution
# ---------------------------------------------------------------------------


class TestEffectiveConfigFourStepResolution(unittest.TestCase):
    """Validates the four-step resolution chain for v4 entries."""

    _MAC = "aa:bb:cc:dd:ee:04"
    _LEGACY_IP = "5.6.7.8"

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_step_1_cross_platform_per_miner_wins(self):
        """Step 1: PASSWORD at MINER_CONFIGS[mac] top-level beats fleet_ops PASSWORD.

        Also verifies the whitelist gate: a non-whitelisted key at the same top-level
        position must NOT be returned via step 1 (KeyError expected).
        """
        state.CONFIG["fleet_ops"]["PASSWORD"] = "fleet-password"
        entry = _v4_entry("10.0.0.1", PASSWORD="miner-password")
        # Add a non-whitelisted top-level key — must NOT be returned via step 1.
        entry["NOT_WHITELISTED"] = "should-not-leak"
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        # Positive: whitelisted key is returned
        self.assertEqual(cfg["PASSWORD"], "miner-password")
        # Negative: non-whitelisted key at top level must NOT resolve via step 1
        with self.assertRaises(KeyError):
            _ = cfg["NOT_WHITELISTED"]

    def test_step_2_per_platform_per_miner_wins_over_default(self):
        """Step 2: VOLTAGE_MV in platforms[epic] beats CONFIG defaults[epic][VOLTAGE_MV]."""
        _set_default("VOLTAGE_MV", 14000, platform="epic")
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            "10.0.0.1",
            platforms={"epic": {"VOLTAGE_MV": 13000}},
        )
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["VOLTAGE_MV"], 13000)

    def test_step_3_per_platform_default_wins_over_fleet_ops(self):
        """Step 3: per-platform default beats fleet_ops for a platform-scoped key."""
        _set_default("SOME_KEY", 99, platform="epic")
        state.CONFIG["fleet_ops"]["SOME_KEY"] = 11
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["SOME_KEY"], 99)

    def test_step_4_fleet_ops_singleton(self):
        """Step 4: key only in fleet_ops is returned when absent from all prior steps."""
        state.CONFIG["fleet_ops"]["SOME_KEY"] = 55
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["SOME_KEY"], 55)

    def test_resolution_uses_current_firmware_for_v4_entry(self):
        """v4 entry with current_firmware='bixbit' reads from defaults[bixbit], not [epic]."""
        _set_default("CHIP_FREQ_SPREAD_MHZ", 60, platform="epic")
        _set_default("CHIP_FREQ_SPREAD_MHZ", 80, platform="bixbit")
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1", firmware="bixbit")
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["CHIP_FREQ_SPREAD_MHZ"], 80)

    def test_resolution_uses_firmware_type_for_legacy_v3_entry(self):
        """Legacy v3 entry with firmware_type='bixbit' reads from defaults[bixbit]."""
        _set_default("CHIP_FREQ_SPREAD_MHZ", 60, platform="epic")
        _set_default("CHIP_FREQ_SPREAD_MHZ", 80, platform="bixbit")
        state.MINER_CONFIGS[self._LEGACY_IP] = {"firmware_type": "bixbit"}
        cfg = EffectiveConfig(self._LEGACY_IP)
        self.assertEqual(cfg["CHIP_FREQ_SPREAD_MHZ"], 80)

    def test_resolution_priority_full_chain(self):
        """All four sources populated — step-1 cross-platform top-level key wins over step-2.

        PASSWORD is in CROSS_PLATFORM_PER_MINER_KEYS (step 1).
        platforms[epic][PASSWORD] is the step-2 per-platform override.
        fleet_ops[PASSWORD] is the step-4 fallback.
        Step 1 must beat step 2 must beat step 4.
        """
        # Step 4: fleet_ops
        state.CONFIG["fleet_ops"]["PASSWORD"] = "fleet"
        # Step 2: per-platform override — populated to verify step 1 beats it
        entry = _v4_entry(
            "10.0.0.1",
            platforms={"epic": {"PASSWORD": "platform-password"}},
            PASSWORD="per-miner",
        )
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        # Step 1 (top-level cross-platform "per-miner") must win over step 2
        self.assertEqual(cfg["PASSWORD"], "per-miner")

    def test_step_2_platform_override_not_in_platforms_dict(self):
        """When platforms dict exists but the key is absent, falls through to step 3."""
        _set_default("VOLTAGE_MV", 9999, platform="epic")
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            "10.0.0.1",
            platforms={"epic": {}},  # key absent
        )
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["VOLTAGE_MV"], 9999)

    def test_step_3_non_epic_default_resolved_via_current_firmware(self):
        """v4 entry firmware=bixbit, key absent from platforms[bixbit] → bixbit default returned."""
        _set_default("VOLTAGE_MV", 14000, platform="epic")
        _set_default("VOLTAGE_MV", 13000, platform="bixbit")
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            "10.0.0.1",
            firmware="bixbit",
            platforms={"bixbit": {}},
        )
        cfg = EffectiveConfig(self._MAC)
        # Must return bixbit default (13000), NOT epic default (14000)
        self.assertEqual(cfg["VOLTAGE_MV"], 13000)


# ---------------------------------------------------------------------------
# 5. TestEffectiveConfigContains
# ---------------------------------------------------------------------------


class TestEffectiveConfigContains(unittest.TestCase):
    """__contains__ follows the same 4-step resolution chain."""

    _MAC = "aa:bb:cc:dd:ee:05"

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_contains_finds_in_per_platform_override(self):
        """Key in platforms[epic] dict → True."""
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            "10.0.0.1",
            platforms={"epic": {"VOLTAGE_MV": 14000}},
        )
        cfg = EffectiveConfig(self._MAC)
        self.assertIn("VOLTAGE_MV", cfg)

    def test_contains_finds_in_cross_platform_override(self):
        """Top-level PASSWORD in MINER_CONFIGS[mac] → True via cross-platform step.

        Also verifies the whitelist gate: a non-whitelisted key at the same top-level
        position must NOT be found via step 1 (__contains__ returns False).
        """
        entry = _v4_entry("10.0.0.1", PASSWORD="pw")
        # Add a non-whitelisted top-level key — must NOT be found via step 1.
        entry["NOT_WHITELISTED"] = "leak"
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        # Positive: whitelisted key is found
        self.assertIn("PASSWORD", cfg)
        # Negative: non-whitelisted top-level key must NOT be found via step 1
        self.assertNotIn("NOT_WHITELISTED", cfg)

    def test_contains_finds_in_default(self):
        """Key only in CONFIG defaults → True."""
        _set_default("SOME_KEY", 5)
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertIn("SOME_KEY", cfg)

    def test_contains_finds_in_fleet_ops(self):
        """Key only in fleet_ops → True."""
        state.CONFIG["fleet_ops"]["ANOTHER_KEY"] = 7
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertIn("ANOTHER_KEY", cfg)

    def test_contains_returns_false_for_absent_key(self):
        """Key absent from all sources → False."""
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertNotIn("NOT_A_KEY", cfg)

    def test_contains_acquires_lock(self):
        """__contains__ acquires state.config_lock at least once."""
        _set_default("KEY", 1)
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter
        try:
            cfg = EffectiveConfig(self._MAC)
            count_before = counter.acquire_count
            _ = "KEY" in cfg
            self.assertGreater(counter.acquire_count, count_before)
        finally:
            state.config_lock = original


# ---------------------------------------------------------------------------
# 6. TestEffectiveConfigGetMethod
# ---------------------------------------------------------------------------


class TestEffectiveConfigGetMethod(unittest.TestCase):
    """get() follows the same chain; returns a default on miss instead of raising."""

    _MAC = "aa:bb:cc:dd:ee:06"

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_get_returns_value_when_present(self):
        """get() returns the resolved value when the key is found."""
        _set_default("SOME_KEY", 42)
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg.get("SOME_KEY"), 42)

    def test_get_returns_default_when_absent(self):
        """get() returns the supplied default (None by default) when the key is absent."""
        state.MINER_CONFIGS[self._MAC] = _v4_entry("10.0.0.1")
        cfg = EffectiveConfig(self._MAC)
        self.assertIsNone(cfg.get("NOT_A_KEY"))
        self.assertEqual(cfg.get("NOT_A_KEY", "fallback"), "fallback")

    def test_get_per_platform_override_via_v4(self):
        """get() resolves per-platform override correctly for v4 entries."""
        _set_default("VOLTAGE_MV", 14000)
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            "10.0.0.1",
            platforms={"epic": {"VOLTAGE_MV": 13500}},
        )
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg.get("VOLTAGE_MV"), 13500)


# ---------------------------------------------------------------------------
# 7. TestEffectiveConfigBackwardCompat
# ---------------------------------------------------------------------------


class TestEffectiveConfigBackwardCompat(unittest.TestCase):
    """v3 test-fixture injection pattern keeps working via the legacy fallback path."""

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_v3_test_fixture_pattern_still_works(self):
        """Mimics existing test_effective_config.py: MINER_CONFIGS["1.2.3.4"] = {flat v3}."""
        state.MINER_CONFIGS["1.2.3.4"] = {"SOME_KEY": 999, "firmware_type": "epic"}
        cfg = EffectiveConfig("1.2.3.4")
        self.assertEqual(cfg["SOME_KEY"], 999)

    def test_v3_falls_through_to_platform_default(self):
        """v3 IP-keyed entry without a per-miner key falls through to platform default."""
        _set_default("ANOTHER_KEY", 55)
        state.MINER_CONFIGS["2.3.4.5"] = {"firmware_type": "epic"}
        cfg = EffectiveConfig("2.3.4.5")
        self.assertEqual(cfg["ANOTHER_KEY"], 55)

    def test_v3_override_beats_default(self):
        """v3 flat per-miner override beats the platform default."""
        _set_default("KEY", 100)
        state.MINER_CONFIGS["3.4.5.6"] = {"firmware_type": "epic", "KEY": 777}
        cfg = EffectiveConfig("3.4.5.6")
        self.assertEqual(cfg["KEY"], 777)

    def test_v3_mutation_visible_immediately(self):
        """Mutations to an IP-keyed v3 entry are visible on the next read (no caching)."""
        _set_default("KEY", 100)
        cfg = EffectiveConfig("4.5.6.7")
        self.assertEqual(cfg["KEY"], 100)
        state.MINER_CONFIGS["4.5.6.7"] = {"firmware_type": "epic", "KEY": 555}
        self.assertEqual(cfg["KEY"], 555)


# ---------------------------------------------------------------------------
# 8. TestEffectiveConfigCrossPlatformWhitelist
# ---------------------------------------------------------------------------


class TestEffectiveConfigCrossPlatformWhitelist(unittest.TestCase):
    """Only CROSS_PLATFORM_PER_MINER_KEYS members at v4 top level resolve via step 1.

    Any other key at the top level of a v4 entry is NOT surfaced by step 1 —
    per-platform tuning overrides belong under platforms[fw][key].
    """

    _MAC = "aa:bb:cc:dd:ee:07"

    def setUp(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        _clear_test_keys()

    def test_unknown_top_level_key_in_v4_not_resolved_via_cross_platform_path(self):
        """RANDOM_KEY at the v4 top level (not in CROSS_PLATFORM_PER_MINER_KEYS) is NOT
        returned by __getitem__ when no platform default or fleet_ops entry exists."""
        entry = _v4_entry("10.0.0.1")
        entry["RANDOM_KEY"] = 999  # top-level but NOT in CROSS_PLATFORM_PER_MINER_KEYS
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        # Must NOT return 999; must raise KeyError (key absent from platforms/defaults/fleet_ops)
        with self.assertRaises(KeyError):
            _ = cfg["RANDOM_KEY"]

    def test_v4_entry_per_platform_override_separate_from_cross_platform(self):
        """RANDOM_KEY in platforms[epic] IS resolved correctly via step 2."""
        state.MINER_CONFIGS[self._MAC] = _v4_entry(
            "10.0.0.1",
            platforms={"epic": {"RANDOM_KEY": 777}},
        )
        cfg = EffectiveConfig(self._MAC)
        self.assertEqual(cfg["RANDOM_KEY"], 777)

    def test_cross_platform_keys_are_correct_set(self):
        """Verify CROSS_PLATFORM_PER_MINER_KEYS matches the spec-required members."""
        self.assertIsInstance(CROSS_PLATFORM_PER_MINER_KEYS, frozenset)
        self.assertEqual(
            CROSS_PLATFORM_PER_MINER_KEYS,
            frozenset({"PASSWORD", "MRR_RIG_ID", "hostname", "current_firmware"}),
        )

    def test_all_cross_platform_keys_resolve_from_top_level(self):
        """Every key in CROSS_PLATFORM_PER_MINER_KEYS resolves when set at the v4 top level."""
        entry = _v4_entry("10.0.0.1")
        expected = {
            "PASSWORD": "pw123",
            "MRR_RIG_ID": 42,
            "hostname": "miner-x",
            "current_firmware": "epic",
        }
        # All four CROSS_PLATFORM_PER_MINER_KEYS members are explicitly tested,
        # including current_firmware which _v4_entry() already sets to "epic".
        entry.update(expected)
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        for key, val in expected.items():
            with self.subTest(key=key):
                self.assertEqual(cfg[key], val)

    def test_contains_false_for_unknown_top_level_key_with_no_default(self):
        """__contains__ returns False for v4 top-level key not in CROSS_PLATFORM_PER_MINER_KEYS
        when there is no platform default or fleet_ops entry."""
        entry = _v4_entry("10.0.0.1")
        entry["SPURIOUS_KEY"] = 123
        state.MINER_CONFIGS[self._MAC] = entry
        cfg = EffectiveConfig(self._MAC)
        self.assertNotIn("SPURIOUS_KEY", cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
