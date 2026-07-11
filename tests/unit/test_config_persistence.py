"""Unit tests for v1/v2 → v3 → v4 CONFIG migration in tuner_app.config.persistence.

Covers:
- test_v2_to_v3_migration: v2 flat defaults are partitioned into per-platform
  buckets (per-platform keys) and fleet_ops (fleet-only keys).
- test_v1_to_v3_migration: v1 flat top-level dict is migrated the same way.
- test_v3_load_passthrough: v3 file loads directly into platform buckets and
  fleet_ops with no mutation of unrelated keys.
- test_v3_per_platform_bucket_loaded_correctly: each platform bucket is
  independently populated from the v3 file.
- test_orphan_cleanup_removes_stale_entry: MINER_CONFIGS entry for a MAC whose
  ip field is no longer in MINER_IPS is pruned on load. After v4 migration the
  dict keys are MACs, not IPs, so orphan cleanup must compare the entry's `ip`
  field against MINER_IPS rather than comparing the dict key directly.
- test_firmware_type_backfilled_on_migration: per-miner config lacking
  current_firmware gets "epic" backfilled after v3→v4 migration.
- test_password_per_miner_exempt_from_stale_cleanup: per-miner PASSWORD
  override survives the FLEET_OPS_KEYS stale-key sweep and lands at the
  v4 entry top level.
- test_fleet_ops_key_pruned_from_per_miner: SCAN_INTERVAL_MIN as a stale
  per-miner key is pruned by the stale-key sweep.

v3→v4 migration note: load_config_from_disk now runs a v3→v4 migration step
that re-keys MINER_CONFIGS from IP to MAC (resolved via resolve_mac /
synthesize_mac_id). Tests that load miner_configs must mock these helpers
at their import site inside persistence.py and assert on the MAC-keyed shape.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tuner_app import state
from tuner_app.config import persistence
from tuner_app.config.defaults import apply_defaults

# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------


def _load_v2(
    defaults_data,
    miner_configs=None,
    fleet_ips=None,
    tmp_dir=None,
    resolve_mac_rv="aa:bb:cc:dd:ee:01",
    synthesize_mac_rv="syn-fallback-00000000",
):
    """Write a v2 config file and call load_config_from_disk. Returns (tmp_cfg_path, data_dir)."""
    payload = {
        "version": 2,
        "defaults": defaults_data,
        "miner_configs": miner_configs or {},
        "auth": {},
    }
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=tmp_dir) as f:
        json.dump(payload, f)
        tmp = f.name
    if isinstance(resolve_mac_rv, list):
        rm_kwargs = {"side_effect": resolve_mac_rv}
    else:
        rm_kwargs = {"return_value": resolve_mac_rv}
    with ExitStack() as stack:
        stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", tmp))
        stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.constants.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.config.persistence.resolve_mac", **rm_kwargs))
        stack.enter_context(
            patch(
                "tuner_app.config.persistence.synthesize_mac_id",
                return_value=synthesize_mac_rv,
            )
        )
        persistence.load_config_from_disk()
    return tmp, tmp_dir


def _load_v1(
    flat_data,
    miner_configs=None,
    tmp_dir=None,
    resolve_mac_rv="aa:bb:cc:dd:ee:01",
    synthesize_mac_rv="syn-fallback-00000000",
):
    """Write a v1 config file (flat top-level dict) and call load_config_from_disk."""
    payload = dict(flat_data)
    if miner_configs:
        # v1 files didn't have miner_configs — use v2 shape but version=1
        payload["miner_configs"] = miner_configs
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=tmp_dir) as f:
        json.dump(payload, f)
        tmp = f.name
    if isinstance(resolve_mac_rv, list):
        rm_kwargs = {"side_effect": resolve_mac_rv}
    else:
        rm_kwargs = {"return_value": resolve_mac_rv}
    with ExitStack() as stack:
        stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", tmp))
        stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.constants.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.config.persistence.resolve_mac", **rm_kwargs))
        stack.enter_context(
            patch(
                "tuner_app.config.persistence.synthesize_mac_id",
                return_value=synthesize_mac_rv,
            )
        )
        persistence.load_config_from_disk()
    return tmp, tmp_dir


def _load_v3(
    defaults_by_platform,
    fleet_ops,
    miner_configs=None,
    tmp_dir=None,
    resolve_mac_rv="aa:bb:cc:dd:ee:01",
    synthesize_mac_rv="syn-fallback-00000000",
):
    """Write a v3 config file and call load_config_from_disk."""
    payload = {
        "version": 3,
        "defaults": defaults_by_platform,
        "fleet_ops": fleet_ops,
        "miner_configs": miner_configs or {},
        "auth": {},
    }
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=tmp_dir) as f:
        json.dump(payload, f)
        tmp = f.name
    if isinstance(resolve_mac_rv, list):
        rm_kwargs = {"side_effect": resolve_mac_rv}
    else:
        rm_kwargs = {"return_value": resolve_mac_rv}
    with ExitStack() as stack:
        stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", tmp))
        stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.constants.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.config.persistence.resolve_mac", **rm_kwargs))
        stack.enter_context(
            patch(
                "tuner_app.config.persistence.synthesize_mac_id",
                return_value=synthesize_mac_rv,
            )
        )
        persistence.load_config_from_disk()
    return tmp, tmp_dir


def _load_v2_no_version(
    defaults_data,
    miner_configs=None,
    tmp_dir=None,
    resolve_mac_rv="aa:bb:cc:dd:ee:01",
    synthesize_mac_rv="syn-fallback-00000000",
):
    """Write a v2 config WITHOUT the 'version' field and call load_config_from_disk.

    Simulates a hand-edited or partial-write-recovery config that has the v2
    shape (flat 'defaults' dict) but lacks the version field.
    """
    payload = {
        "defaults": defaults_data,
        "miner_configs": miner_configs or {},
        "auth": {},
    }
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=tmp_dir) as f:
        json.dump(payload, f)
        tmp = f.name
    if isinstance(resolve_mac_rv, list):
        rm_kwargs = {"side_effect": resolve_mac_rv}
    else:
        rm_kwargs = {"return_value": resolve_mac_rv}
    with ExitStack() as stack:
        stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", tmp))
        stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.constants.DATA_DIR", tmp_dir))
        stack.enter_context(patch("tuner_app.config.persistence.resolve_mac", **rm_kwargs))
        stack.enter_context(
            patch(
                "tuner_app.config.persistence.synthesize_mac_id",
                return_value=synthesize_mac_rv,
            )
        )
        persistence.load_config_from_disk()
    return tmp, tmp_dir


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestV2ToV3Migration(unittest.TestCase):
    """v2 flat defaults are correctly partitioned into per-platform buckets + fleet_ops."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp_dirs = []

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov
        import shutil

        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mktmp(self):
        import tempfile

        d = tempfile.mkdtemp()
        self._tmp_dirs.append(d)
        return d

    def test_v2_to_v3_migration_fleet_ops_key(self):
        """Fleet-ops key (SCAN_INTERVAL_MIN) from v2 lands in fleet_ops."""
        tmp, _ = _load_v2({"SCAN_INTERVAL_MIN": 42}, tmp_dir=self._mktmp())
        try:
            self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_INTERVAL_MIN"], 42)
        finally:
            os.unlink(tmp)

    def test_v2_to_v3_migration_per_platform_key(self):
        """Per-platform key (BOARD_MAX_TEMP) fans out to all four platform buckets."""
        tmp, _ = _load_v2({"BOARD_MAX_TEMP": 78}, tmp_dir=self._mktmp())
        try:
            for p in ("epic", "bixbit", "luxos", "braiins"):
                self.assertEqual(
                    state.CONFIG["defaults"][p]["BOARD_MAX_TEMP"],
                    78,
                    f"BOARD_MAX_TEMP not in {p} bucket",
                )
        finally:
            os.unlink(tmp)

    def test_v2_to_v3_migration_mixed_keys(self):
        """Fleet + per-platform keys in a v2 file land in the right places."""
        tmp, _ = _load_v2(
            {
                "SCAN_INTERVAL_MIN": 15,
                "BOARD_MAX_TEMP": 75,
                "MINER_IPS": ["10.0.0.1"],
            },
            tmp_dir=self._mktmp(),
        )
        try:
            fo = state.CONFIG["fleet_ops"]
            self.assertEqual(fo["SCAN_INTERVAL_MIN"], 15)
            self.assertEqual(fo["MINER_IPS"], ["10.0.0.1"])
            for p in ("epic", "bixbit", "luxos", "braiins"):
                self.assertEqual(state.CONFIG["defaults"][p]["BOARD_MAX_TEMP"], 75)
        finally:
            os.unlink(tmp)

    def test_v2_unknown_key_silently_dropped(self):
        """A key absent from current CONFIG_DEFAULTS is silently dropped."""
        tmp, _ = _load_v2({"OBSOLETE_LEGACY_KEY_XYZ": 999}, tmp_dir=self._mktmp())
        try:
            # Key must not appear anywhere in v3 state
            self.assertNotIn("OBSOLETE_LEGACY_KEY_XYZ", state.CONFIG["fleet_ops"])
            for p in ("epic", "bixbit", "luxos", "braiins"):
                self.assertNotIn("OBSOLETE_LEGACY_KEY_XYZ", state.CONFIG["defaults"][p])
        finally:
            os.unlink(tmp)


class TestV1ToV3Migration(unittest.TestCase):
    """v1 flat top-level dict is migrated the same way as v2."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp_dirs = []

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov
        import shutil

        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mktmp(self):
        import tempfile

        d = tempfile.mkdtemp()
        self._tmp_dirs.append(d)
        return d

    def test_v1_fleet_ops_key_migrated(self):
        """Fleet-ops key from a v1 file lands in fleet_ops."""
        tmp, _ = _load_v1({"SCAN_INTERVAL_MIN": 5}, tmp_dir=self._mktmp())
        try:
            self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_INTERVAL_MIN"], 5)
        finally:
            os.unlink(tmp)

    def test_v1_per_platform_key_fans_out(self):
        """Per-platform key from a v1 file fans out to all platform buckets."""
        tmp, _ = _load_v1({"BOARD_MAX_TEMP": 72}, tmp_dir=self._mktmp())
        try:
            for p in ("epic", "bixbit", "luxos", "braiins"):
                self.assertEqual(state.CONFIG["defaults"][p]["BOARD_MAX_TEMP"], 72)
        finally:
            os.unlink(tmp)

    def test_v1_password_migrates_to_scan_passwords(self):
        """v1 PASSWORD key is migrated to SCAN_PASSWORDS[0] in fleet_ops."""
        tmp, _ = _load_v1(
            {"PASSWORD": "oldpass", "SCAN_PASSWORDS": ["letmein"]},
            tmp_dir=self._mktmp(),
        )
        try:
            fo = state.CONFIG["fleet_ops"]
            self.assertEqual(fo["SCAN_PASSWORDS"][0], "oldpass")
            self.assertIn("letmein", fo["SCAN_PASSWORDS"])
        finally:
            os.unlink(tmp)


class TestV3LoadPassthrough(unittest.TestCase):
    """v3 file loads directly into platform buckets + fleet_ops unchanged."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp_dirs = []

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov
        import shutil

        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mktmp(self):
        import tempfile

        d = tempfile.mkdtemp()
        self._tmp_dirs.append(d)
        return d

    def test_v3_load_passthrough_fleet_ops(self):
        """fleet_ops key from v3 file lands in fleet_ops unchanged."""
        tmp, _ = _load_v3(
            defaults_by_platform={
                "epic": {},
                "bixbit": {},
                "luxos": {},
                "braiins": {},
            },
            fleet_ops={"SCAN_INTERVAL_MIN": 99},
            tmp_dir=self._mktmp(),
        )
        try:
            self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_INTERVAL_MIN"], 99)
        finally:
            os.unlink(tmp)

    def test_v3_load_passthrough_per_platform_bucket(self):
        """Per-platform key in v3 epic bucket is loaded into the epic bucket only."""
        tmp, _ = _load_v3(
            defaults_by_platform={
                "epic": {"BOARD_MAX_TEMP": 83},
                "bixbit": {"BOARD_MAX_TEMP": 77},
                "luxos": {},
                "braiins": {},
            },
            fleet_ops={},
            tmp_dir=self._mktmp(),
        )
        try:
            self.assertEqual(state.CONFIG["defaults"]["epic"]["BOARD_MAX_TEMP"], 83)
            self.assertEqual(state.CONFIG["defaults"]["bixbit"]["BOARD_MAX_TEMP"], 77)
            # luxos and braiins stay at their apply_defaults values (not changed)
        finally:
            os.unlink(tmp)

    def test_v3_each_platform_bucket_loaded_independently(self):
        """Four platform buckets load independently from v3 — no cross-pollution."""
        apply_defaults()
        epic_max_temp = state.CONFIG["defaults"]["epic"]["BOARD_MAX_TEMP"]
        tmp, _ = _load_v3(
            defaults_by_platform={
                "epic": {"BOARD_MAX_TEMP": 60},
                "bixbit": {},
                "luxos": {},
                "braiins": {},
            },
            fleet_ops={},
            tmp_dir=self._mktmp(),
        )
        try:
            # epic changed
            self.assertEqual(state.CONFIG["defaults"]["epic"]["BOARD_MAX_TEMP"], 60)
            # others stay at defaults
            self.assertEqual(state.CONFIG["defaults"]["bixbit"]["BOARD_MAX_TEMP"], epic_max_temp)
        finally:
            os.unlink(tmp)

    def test_v3_miner_configs_loaded(self):
        """miner_configs from v3 file are loaded into state.MINER_CONFIGS as MAC-keyed v4 entries.

        After the v3→v4 migration, the entry is keyed by the resolved MAC, not by IP.
        The entry carries current_firmware (renamed from firmware_type) at the top level.
        """
        tmp, _ = _load_v3(
            defaults_by_platform={
                "epic": {},
                "bixbit": {},
                "luxos": {},
                "braiins": {},
            },
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "bixbit"}},
            tmp_dir=self._mktmp(),
            resolve_mac_rv="aa:bb:cc:dd:ee:01",
        )
        try:
            self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
            self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
            entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
            self.assertEqual(entry["current_firmware"], "bixbit")
            self.assertEqual(entry["ip"], "10.0.0.5")
        finally:
            os.unlink(tmp)


class TestOrphanCleanup(unittest.TestCase):
    """Orphan cleanup removes MINER_CONFIGS entries whose ip field is not in MINER_IPS."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp_dirs = []

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov
        import shutil

        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mktmp(self):
        import tempfile

        d = tempfile.mkdtemp()
        self._tmp_dirs.append(d)
        return d

    def test_orphan_ip_removed(self):
        """MAC entry whose ip field is absent from MINER_IPS is pruned after migration.

        Orphan cleanup uses the entry's `ip` field (NOT the dict key) to detect stale
        per-miner entries after v4 migration. The surviving entry's `ip` field must
        equal the valid IP in MINER_IPS.
        """
        tmp, _ = _load_v3(
            defaults_by_platform={p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
            fleet_ops={"MINER_IPS": ["10.0.0.1"]},
            miner_configs={
                "10.0.0.1": {"firmware_type": "epic"},
                "10.0.0.99": {"firmware_type": "epic"},  # orphan
            },
            tmp_dir=self._mktmp(),
            # Two IPs → two calls to resolve_mac
            resolve_mac_rv=["aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:99"],
        )
        try:
            # MAC for 10.0.0.1 survives
            self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
            # Surviving entry's ip field must equal the valid IP in MINER_IPS
            self.assertEqual(
                state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]["ip"],
                "10.0.0.1",
                "Surviving entry ip field must match the valid MINER_IPS entry",
            )
            # MAC for orphan 10.0.0.99 is pruned (its ip field was not in MINER_IPS)
            self.assertNotIn("aa:bb:cc:dd:ee:99", state.MINER_CONFIGS, "orphan MAC must be pruned")
        finally:
            os.unlink(tmp)

    def test_firmware_type_backfilled(self):
        """Per-miner config lacking current_firmware gets 'epic' backfilled after v3→v4 migration."""
        tmp, _ = _load_v3(
            defaults_by_platform={p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
            fleet_ops={"MINER_IPS": ["10.0.0.2"]},
            miner_configs={"10.0.0.2": {"PASSWORD": "letmein"}},
            tmp_dir=self._mktmp(),
            resolve_mac_rv="aa:bb:cc:dd:ee:02",
        )
        try:
            entry = state.MINER_CONFIGS.get("aa:bb:cc:dd:ee:02", {})
            # current_firmware is backfilled to "epic"
            self.assertEqual(entry.get("current_firmware"), "epic")
        finally:
            os.unlink(tmp)

    def test_password_per_miner_exempt_from_stale_cleanup(self):
        """Per-miner PASSWORD override lands at the v4 entry top level and is not pruned."""
        tmp, _ = _load_v3(
            defaults_by_platform={p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
            fleet_ops={"MINER_IPS": ["10.0.0.3"]},
            miner_configs={"10.0.0.3": {"PASSWORD": "minerpass", "firmware_type": "epic"}},
            tmp_dir=self._mktmp(),
            resolve_mac_rv="aa:bb:cc:dd:ee:03",
        )
        try:
            entry = state.MINER_CONFIGS.get("aa:bb:cc:dd:ee:03", {})
            # PASSWORD at the top level (cross-platform key)
            self.assertIn("PASSWORD", entry)
            self.assertEqual(entry["PASSWORD"], "minerpass")
        finally:
            os.unlink(tmp)

    def test_fleet_ops_key_pruned_from_per_miner(self):
        """A fleet-only key (SCAN_INTERVAL_MIN) in a per-miner override is pruned from the v4 entry."""
        tmp, _ = _load_v3(
            defaults_by_platform={p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
            fleet_ops={"MINER_IPS": ["10.0.0.4"]},
            miner_configs={
                "10.0.0.4": {
                    "firmware_type": "epic",
                    "SCAN_INTERVAL_MIN": 99,  # fleet-ops only; stale in per-miner
                }
            },
            tmp_dir=self._mktmp(),
            resolve_mac_rv="aa:bb:cc:dd:ee:04",
        )
        try:
            entry = state.MINER_CONFIGS.get("aa:bb:cc:dd:ee:04", {})
            self.assertNotIn(
                "SCAN_INTERVAL_MIN",
                entry,
                "SCAN_INTERVAL_MIN is fleet-ops-only and must be pruned from per-miner config",
            )
            # Also must not be inside platforms
            platforms = entry.get("platforms", {})
            for platform_data in platforms.values():
                self.assertNotIn(
                    "SCAN_INTERVAL_MIN",
                    platform_data,
                    "SCAN_INTERVAL_MIN must not appear in the platforms sub-dict either",
                )
        finally:
            os.unlink(tmp)


class TestV2WithoutVersionField(unittest.TestCase):
    """Regression: v2 configs written without a 'version' field must still load correctly.

    Pre-Phase-1 code sometimes omitted the version field on hand-edits.
    The new shape-based detection must catch them as v2 (not fall through
    to v1 where 'defaults', 'miner_configs', 'auth' would be treated as
    unknown top-level keys and silently dropped).
    """

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp_dirs = []

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov
        import shutil

        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mktmp(self):
        import tempfile

        d = tempfile.mkdtemp()
        self._tmp_dirs.append(d)
        return d

    def test_v2_without_version_field_still_partitioned(self):
        """v2 file without 'version' key: per-platform key fans out to all 4 buckets;
        fleet-ops key lands in fleet_ops.  Neither 'defaults' nor 'miner_configs' nor
        'auth' appear as phantom config keys."""
        tmp, _ = _load_v2_no_version(
            {"BOARD_MAX_TEMP": 72, "MINER_IPS": ["1.2.3.4"]},
            tmp_dir=self._mktmp(),
        )
        try:
            # BOARD_MAX_TEMP is a per-platform key — must fan out to all buckets
            for p in ("epic", "bixbit", "luxos", "braiins"):
                self.assertEqual(
                    state.CONFIG["defaults"][p]["BOARD_MAX_TEMP"],
                    72,
                    f"BOARD_MAX_TEMP must be in {p} bucket",
                )
            # MINER_IPS is a fleet-ops key — must land in fleet_ops
            self.assertEqual(state.CONFIG["fleet_ops"]["MINER_IPS"], ["1.2.3.4"])
            # The structural keys 'defaults'/'miner_configs'/'auth' must NOT appear
            # anywhere in the CONFIG (they are schema structure, not config values).
            for phantom in ("defaults", "miner_configs", "auth", "version"):
                self.assertNotIn(phantom, state.CONFIG["fleet_ops"], phantom)
                for p in ("epic", "bixbit", "luxos", "braiins"):
                    self.assertNotIn(phantom, state.CONFIG["defaults"][p], phantom)
        finally:
            os.unlink(tmp)


class TestBoundsClampingPerPlatformBucket(unittest.TestCase):
    """Bounds clamping after v3 load must iterate over each platform bucket independently.

    Issue 4: No cross-bucket leakage — clamping epic's value must not alter bixbit's.
    """

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {ip: dict(ov) for ip, ov in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp_dirs = []

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for ip, ov in self._saved_miner_configs.items():
            state.MINER_CONFIGS[ip] = ov
        import shutil

        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _mktmp(self):
        import tempfile

        d = tempfile.mkdtemp()
        self._tmp_dirs.append(d)
        return d

    def test_bounds_clamping_per_platform_bucket_independent(self):
        """Two different out-of-bounds values for the same key in two platform buckets
        are each clamped to the upper bound independently, with no cross-bucket leak."""
        from tuner_app.config.schema import CONFIG_BOUNDS

        key = "CHIP_FREQ_SPREAD_MHZ"
        lo, hi = CONFIG_BOUNDS[key]

        tmp, _ = _load_v3(
            defaults_by_platform={
                "epic": {key: 999999},
                "bixbit": {key: 888888},
                "luxos": {},
                "braiins": {},
            },
            fleet_ops={},
            tmp_dir=self._mktmp(),
        )
        try:
            epic_val = state.CONFIG["defaults"]["epic"][key]
            bixbit_val = state.CONFIG["defaults"]["bixbit"][key]
            # Both clamped to the upper bound
            self.assertEqual(epic_val, hi, f"epic {key} must be clamped to {hi}")
            self.assertEqual(bixbit_val, hi, f"bixbit {key} must be clamped to {hi}")
            # Values are independent — both hit the SAME cap, but from different
            # starting values; neither one should have leaked into the other.
            # The key assertion: if clamping were shared/cross-bucket we'd see
            # only a single clamp applied rather than two independent ones.
            # We can verify independence by checking the luxos bucket stayed at
            # apply_defaults value (not clamped from either epic or bixbit).
            default_val = state.CONFIG["defaults"]["luxos"][key]
            self.assertLessEqual(default_val, hi, f"luxos {key} within bounds")
            self.assertGreaterEqual(default_val, lo, f"luxos {key} within bounds")
            # The luxos default must not have been set to 999999 or 888888
            self.assertNotEqual(default_val, 999999)
            self.assertNotEqual(default_val, 888888)
        finally:
            os.unlink(tmp)


class TestSecureConfigPersistence(unittest.TestCase):
    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_auth = dict(state.AUTH)
        self._saved_miner_configs = copy.deepcopy(state.MINER_CONFIGS)
        apply_defaults()
        state.AUTH.clear()
        state.AUTH.update({"password_hash": "stored-auth-hash", "created_at": "now"})
        state.MINER_CONFIGS.clear()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self._tmpdir.name, "private-data")
        self.config_file = os.path.join(self.data_dir, "config.json")

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.AUTH.clear()
        state.AUTH.update(self._saved_auth)
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS.update(self._saved_miner_configs)
        self._tmpdir.cleanup()

    def test_config_write_is_atomic_and_owner_only(self):
        state.CONFIG["fleet_ops"]["PASSWORD"] = "persisted-miner-password"
        with (
            patch("tuner_app.config.persistence.DATA_DIR", self.data_dir),
            patch("tuner_app.config.persistence.CONFIG_FILE", self.config_file),
        ):
            persistence.save_config_to_disk()

        self.assertEqual(stat.S_IMODE(os.stat(self.data_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.config_file).st_mode), 0o600)
        with open(self.config_file, encoding="utf-8") as f:
            payload = json.load(f)
        # The protected config is the one permitted persistence boundary.
        self.assertEqual(payload["auth"]["password_hash"], "stored-auth-hash")
        self.assertEqual(payload["fleet_ops"]["PASSWORD"], "persisted-miner-password")
        self.assertFalse(any(name.endswith(".tmp") for name in os.listdir(self.data_dir)))

    def test_corrupt_existing_config_fails_closed(self):
        os.makedirs(self.data_dir)
        with open(self.config_file, "w", encoding="utf-8") as f:
            f.write('{"version": 4, "auth": ')
        with (
            patch("tuner_app.config.persistence.DATA_DIR", self.data_dir),
            patch("tuner_app.config.persistence.CONFIG_FILE", self.config_file),
            self.assertRaises(persistence.ConfigLoadError),
        ):
            persistence.load_config_from_disk()


if __name__ == "__main__":
    unittest.main(verbosity=2)
