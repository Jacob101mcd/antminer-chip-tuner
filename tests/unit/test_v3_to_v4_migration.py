"""Unit tests for v3 → v4 MINER_CONFIGS migration in tuner_app.config.persistence.

Spec contract under test
------------------------
The v3→v4 migration runs as part of load_config_from_disk() immediately after
the in-memory v3 state is built (MINER_CONFIGS keyed by IP) and BEFORE per-miner
override cleanup. It:

1. Re-keys MINER_CONFIGS entries from IP to MAC (resolved via resolve_mac) or a
   synthesized identifier (synthesize_mac_id) when ARP resolution returns None.
2. Partitions v3 per-miner dict fields into:
   - Top-level cross-platform fields, which are of two kinds:
     * Structural fields (`ip`, `id_synthesized`) — added by the migration as
       runtime metadata, NOT members of CROSS_PLATFORM_PER_MINER_KEYS.
     * CROSS_PLATFORM_PER_MINER_KEYS members (`PASSWORD`, `MRR_RIG_ID`,
       `hostname`, `current_firmware`) — configuration-resolvable per-miner
       overrides that survive firmware reflash.
   - Per-platform tuning keys inside platforms[current_firmware].
3. Renames the v3 field "firmware_type" to "current_firmware".
4. Renames tuning files on disk:
   - {ip-dashes}.json → {mac-dashes}.{firmware}.profile.json
   - {ip-dashes}.checkpoint.json → {mac-dashes}.{firmware}.checkpoint.json
   - {ip-dashes}.stock.json → {mac-dashes}.{firmware}.stock.json
   - {ip-dashes}.log.jsonl → {mac-dashes}.log.jsonl  (cross-platform; no firmware suffix)
5. Writes a sentinel file tuning_data/.migration_v3_to_v4.done after migration completes.
6. Skips the migration entirely when the sentinel already exists (idempotency guard A).
7. Skips re-migration of already-migrated individual entries when they have the v4
   shape (`"platforms" in entry or "current_firmware" in entry`) even if the sentinel
   is absent (idempotency guard B — crash-recovery path).
8. Is non-fatal: if a file rename fails (target already exists, etc.), migration
   of in-memory state still completes; only the failing rename is skipped.

The constant CROSS_PLATFORM_PER_MINER_KEYS is added to tuner_app.constants:
    frozenset({"PASSWORD", "MRR_RIG_ID", "hostname", "current_firmware"})

save_config_to_disk writes version: 4 and MAC-keyed miner_configs.

TDD mode: tests FAIL against current codebase (no v4 implementation yet).
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.config import persistence
from tuner_app.config.defaults import apply_defaults

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_v3_config(tmp_dir, defaults_by_platform=None, fleet_ops=None, miner_configs=None):
    """Write a v3 config.json into tmp_dir and return the file path."""
    payload = {
        "version": 3,
        "defaults": defaults_by_platform or {p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
        "fleet_ops": fleet_ops or {},
        "miner_configs": miner_configs or {},
        "auth": {},
    }
    cfg_path = os.path.join(tmp_dir, "config.json")
    with open(cfg_path, "w") as f:
        json.dump(payload, f)
    return cfg_path


def _load_with_patches(cfg_path, data_dir, resolve_mac_rv, synthesize_mac_rv=None):
    """Call load_config_from_disk with CONFIG_FILE and DATA_DIR patched.

    resolve_mac_rv may be a single return value (applied to all calls) or
    a list that is consumed in order.
    synthesize_mac_rv is the return value for synthesize_mac_id (optional).
    """
    # Build the side_effect/return_value for resolve_mac
    if isinstance(resolve_mac_rv, list):
        resolve_mac_side = resolve_mac_rv
        rm_kwargs = {"side_effect": resolve_mac_side}
    else:
        rm_kwargs = {"return_value": resolve_mac_rv}

    sm_kwargs = {"return_value": synthesize_mac_rv or "syn-fallback-00000000"}

    sentinel = os.path.join(data_dir, ".migration_v3_to_v4.done")
    with ExitStack() as stack:
        stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
        stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", data_dir))
        stack.enter_context(patch("tuner_app.constants.DATA_DIR", data_dir))
        stack.enter_context(patch("tuner_app.config.persistence.resolve_mac", **rm_kwargs))
        stack.enter_context(patch("tuner_app.config.persistence.synthesize_mac_id", **sm_kwargs))
        persistence.load_config_from_disk()
    return sentinel


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestCrossPlatformPerMinerKeysConstant(unittest.TestCase):
    """CROSS_PLATFORM_PER_MINER_KEYS is exported from tuner_app.constants with the correct members."""

    def test_cross_platform_per_miner_keys_constant_present(self):
        """CROSS_PLATFORM_PER_MINER_KEYS is a frozenset containing exactly the
        four expected members."""
        from tuner_app.constants import CROSS_PLATFORM_PER_MINER_KEYS

        self.assertIsInstance(CROSS_PLATFORM_PER_MINER_KEYS, frozenset)
        self.assertEqual(
            CROSS_PLATFORM_PER_MINER_KEYS,
            frozenset({"PASSWORD", "MRR_RIG_ID", "hostname", "current_firmware"}),
        )


class TestV3ToV4MigrationBasic(unittest.TestCase):
    """Core migration: IP-keyed v3 entries become MAC-keyed v4 entries."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_v3_to_v4_keys_by_mac_with_resolved_mac(self):
        """v3 entry keyed by IP migrates to MAC key; ip field preserved and id_synthesized is False."""
        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        _load_with_patches(cfg, self._tmp, resolve_mac_rv="aa:bb:cc:dd:ee:01")

        self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
        self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertEqual(entry["current_firmware"], "epic")
        self.assertFalse(entry["id_synthesized"])
        self.assertIn("platforms", entry)

    def test_v3_to_v4_synthesizes_when_resolve_mac_returns_none(self):
        """When resolve_mac returns None, synthesize_mac_id is used; id_synthesized is True."""
        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        _load_with_patches(
            cfg,
            self._tmp,
            resolve_mac_rv=None,
            synthesize_mac_rv="syn-10-0-0-5-deadbeef",
        )

        self.assertIn("syn-10-0-0-5-deadbeef", state.MINER_CONFIGS)
        self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS["syn-10-0-0-5-deadbeef"]
        self.assertTrue(entry["id_synthesized"])
        self.assertEqual(entry["ip"], "10.0.0.5")

    def test_v3_to_v4_renames_firmware_type_to_current_firmware(self):
        """After migration, v4 entry has current_firmware and no firmware_type key at the top level."""
        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "bixbit"}},
        )
        _load_with_patches(cfg, self._tmp, resolve_mac_rv="aa:bb:cc:dd:ee:01")

        entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
        self.assertIn("current_firmware", entry)
        self.assertNotIn("firmware_type", entry)
        self.assertEqual(entry["current_firmware"], "bixbit")

    def test_v3_to_v4_partitions_cross_platform_vs_per_platform_keys(self):
        """Cross-platform keys (PASSWORD, MRR_RIG_ID, hostname) land at the top level; tuning keys go under platforms."""
        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={
                "10.0.0.5": {
                    "firmware_type": "epic",
                    "PASSWORD": "minerpass",
                    "MRR_RIG_ID": 42,
                    "hostname": "miner1",
                    "CHIP_FREQ_SPREAD_MHZ": 80,
                    "BASELINE_VOLTAGE_MV": 14000,
                }
            },
        )
        _load_with_patches(cfg, self._tmp, resolve_mac_rv="aa:bb:cc:dd:ee:01")

        entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
        # Cross-platform keys at the top level
        self.assertEqual(entry["PASSWORD"], "minerpass")
        self.assertEqual(entry["MRR_RIG_ID"], 42)
        self.assertEqual(entry["hostname"], "miner1")
        # Per-platform tuning keys inside platforms[epic]
        platforms = entry["platforms"]
        self.assertIn("epic", platforms)
        self.assertEqual(platforms["epic"]["CHIP_FREQ_SPREAD_MHZ"], 80)
        self.assertEqual(platforms["epic"]["BASELINE_VOLTAGE_MV"], 14000)
        # Tuning keys must NOT appear at the top level
        self.assertNotIn("CHIP_FREQ_SPREAD_MHZ", entry)
        self.assertNotIn("BASELINE_VOLTAGE_MV", entry)


class TestV3ToV4MigrationSentinel(unittest.TestCase):
    """Sentinel file controls whether migration runs."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_v3_to_v4_writes_sentinel_after_migration(self):
        """After a successful migration, the sentinel file is written to the data dir."""
        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        sentinel = _load_with_patches(cfg, self._tmp, resolve_mac_rv="aa:bb:cc:dd:ee:01")

        self.assertTrue(os.path.exists(sentinel), "Sentinel file must exist after migration")

    def test_self_heal_runs_migration_when_sentinel_exists_but_state_has_v3_entries(self):
        """Self-heal: stale sentinel does NOT prevent migration when v3-shape IP-keyed entries
        are present. Migration re-keys them; sentinel is then re-written after a successful save."""
        # Pre-write the (stale) sentinel — simulates the deployed-and-broken state
        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("stale")

        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            stack.enter_context(
                patch(
                    "tuner_app.config.persistence.resolve_mac",
                    return_value="aa:bb:cc:dd:ee:01",
                )
            )
            stack.enter_context(
                patch(
                    "tuner_app.config.persistence.synthesize_mac_id", return_value="syn-x-deadbeef"
                )
            )
            persistence.load_config_from_disk()

        # Self-heal must have re-keyed the entry to the resolved MAC
        self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
        self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)


class TestV3ToV4FileRenames(unittest.TestCase):
    """Migration renames tuning_data files from IP-based to MAC-based names."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_tuning_files(self, *names):
        """Create zero-byte placeholder files in the temp data dir."""
        for name in names:
            open(os.path.join(self._tmp, name), "w").close()

    def test_v3_to_v4_renames_profile_files(self):
        """Profile, checkpoint, and stock files are renamed (not copied) from IP-dashes to MAC-dashes.firmware form."""
        self._write_tuning_files(
            "10-0-0-5.json",
            "10-0-0-5.checkpoint.json",
            "10-0-0-5.stock.json",
        )
        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        _load_with_patches(cfg, self._tmp, resolve_mac_rv="aa:bb:cc:dd:ee:01")

        # Destinations must exist
        self.assertTrue(
            os.path.exists(os.path.join(self._tmp, "aa-bb-cc-dd-ee-01.epic.profile.json")),
            "Profile file must be renamed to MAC-dashes.firmware.profile.json",
        )
        self.assertTrue(
            os.path.exists(os.path.join(self._tmp, "aa-bb-cc-dd-ee-01.epic.checkpoint.json")),
            "Checkpoint file must be renamed to MAC-dashes.firmware.checkpoint.json",
        )
        self.assertTrue(
            os.path.exists(os.path.join(self._tmp, "aa-bb-cc-dd-ee-01.epic.stock.json")),
            "Stock file must be renamed to MAC-dashes.firmware.stock.json",
        )

        # Sources must be ABSENT — a copy instead of rename would leave them behind
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp, "10-0-0-5.json")),
            "Source profile file must be removed by rename (not copied)",
        )
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp, "10-0-0-5.checkpoint.json")),
            "Source checkpoint file must be removed by rename (not copied)",
        )
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp, "10-0-0-5.stock.json")),
            "Source stock file must be removed by rename (not copied)",
        )

    def test_v3_to_v4_renames_log_file_without_firmware_suffix(self):
        """Log file is renamed (not copied) to MAC-dashes.log.jsonl with NO firmware suffix."""
        self._write_tuning_files("10-0-0-5.log.jsonl")
        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        _load_with_patches(cfg, self._tmp, resolve_mac_rv="aa:bb:cc:dd:ee:01")

        # Destination must exist
        self.assertTrue(
            os.path.exists(os.path.join(self._tmp, "aa-bb-cc-dd-ee-01.log.jsonl")),
            "Log file must be renamed to MAC-dashes.log.jsonl (no firmware suffix)",
        )
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp, "aa-bb-cc-dd-ee-01.epic.log.jsonl")),
            "Log file must NOT have a firmware suffix",
        )

        # Source must be ABSENT — a copy instead of rename would leave it behind
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp, "10-0-0-5.log.jsonl")),
            "Source log file must be removed by rename (not copied)",
        )

    def test_v3_to_v4_rename_failure_is_non_fatal(self):
        """When a rename target already exists, the migration of in-memory state completes without raising."""
        self._write_tuning_files("10-0-0-5.json")
        # Pre-create the target to cause a name collision
        open(os.path.join(self._tmp, "aa-bb-cc-dd-ee-01.epic.profile.json"), "w").close()

        cfg = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        # Must not raise
        _load_with_patches(cfg, self._tmp, resolve_mac_rv="aa:bb:cc:dd:ee:01")

        # In-memory migration still completed
        self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
        # Source file is left in place (rename was skipped due to collision)
        self.assertTrue(os.path.exists(os.path.join(self._tmp, "10-0-0-5.json")))


class TestV3ToV4Idempotency(unittest.TestCase):
    """Migration idempotency: two independent guards prevent double-migration."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_v3_to_v4_idempotent_on_v4_file(self):
        """Loading a v4 config.json with sentinel present loads MAC-keyed entries without re-migrating."""
        v4_payload = {
            "version": 4,
            "defaults": {p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
            "fleet_ops": {"MINER_IPS": ["10.0.0.5"]},
            "miner_configs": {
                "aa:bb:cc:dd:ee:01": {
                    "ip": "10.0.0.5",
                    "current_firmware": "epic",
                    "id_synthesized": False,
                    "platforms": {"epic": {}},
                }
            },
            "auth": {},
        }
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(v4_payload, f)

        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("done")

        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            mock_rm = stack.enter_context(
                patch(
                    "tuner_app.config.persistence.resolve_mac",
                    return_value="aa:bb:cc:dd:ee:99",
                )
            )
            stack.enter_context(
                patch("tuner_app.config.persistence.synthesize_mac_id", return_value="syn-never")
            )
            persistence.load_config_from_disk()
            mock_rm.assert_not_called()

        # Entry is the one from the v4 file — not a re-migrated version
        self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertEqual(entry["current_firmware"], "epic")
        self.assertFalse(entry["id_synthesized"])

    def test_v4_shape_entries_not_remigrated_without_sentinel(self):
        """v4-shape entries are not re-migrated even when the sentinel file is absent (crash-recovery path).

        Guard B: per-entry v4-shape detection (`'platforms' in entry or 'current_firmware' in entry`)
        must skip re-migration of already-migrated entries independently of the sentinel file.
        Without this guard, a deleted sentinel would cause resolve_mac to be called with a MAC
        string treated as an IP, silently corrupting MINER_CONFIGS keys.
        After guard B runs with zero entries needing migration, the sentinel IS written.
        """
        v4_payload = {
            "version": 4,
            "defaults": {p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
            "fleet_ops": {"MINER_IPS": ["10.0.0.5"]},
            "miner_configs": {
                "aa:bb:cc:dd:ee:01": {
                    "ip": "10.0.0.5",
                    "current_firmware": "epic",
                    "id_synthesized": False,
                    "platforms": {"epic": {"CHIP_FREQ_SPREAD_MHZ": 60}},
                }
            },
            "auth": {},
        }
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(v4_payload, f)

        # Sentinel is intentionally NOT pre-created — simulates crash-recovery scenario
        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        self.assertFalse(os.path.exists(sentinel), "Precondition: no sentinel before load")

        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            mock_rm = stack.enter_context(
                patch(
                    "tuner_app.config.persistence.resolve_mac",
                    return_value="aa:bb:cc:dd:ee:99",
                )
            )
            stack.enter_context(
                patch(
                    "tuner_app.config.persistence.synthesize_mac_id",
                    return_value="syn-corrupted",
                )
            )
            persistence.load_config_from_disk()
            # Guard B must have detected the v4-shape entry and skipped re-migration entirely
            mock_rm.assert_not_called()

        # The entry key must be the original MAC from the file — not re-keyed to a
        # garbage "resolved" value derived from treating the MAC as an IP string
        self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
        self.assertNotIn(
            "aa:bb:cc:dd:ee:99",
            state.MINER_CONFIGS,
            "resolve_mac must not have been called on a v4-shape entry",
        )
        self.assertNotIn(
            "syn-corrupted",
            state.MINER_CONFIGS,
            "synthesize_mac_id must not have been called on a v4-shape entry",
        )

        entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertEqual(entry["current_firmware"], "epic")
        self.assertFalse(entry["id_synthesized"])

        # New post-bug-B-fix spec: sentinel is written ONLY after a successful save, AND only when
        # at least one entry was re-keyed. Zero work → no save → no sentinel. (The stale-sentinel
        # state is harmless because the next load re-runs guard B and again finds zero work.)
        self.assertFalse(
            os.path.exists(sentinel),
            "Sentinel must NOT be written when guard B detected zero work to do",
        )


class TestSaveConfigV4Format(unittest.TestCase):
    """save_config_to_disk writes version 4 and MAC-keyed miner_configs."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _save(self):
        cfg_path = os.path.join(self._tmp, "config.json")
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            persistence.save_config_to_disk()
        return cfg_path

    def test_save_config_writes_v4_format(self):
        """save_config_to_disk writes version: 4 and the miner_configs dict uses MAC keys."""
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"] = {
            "ip": "10.0.0.5",
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {"CHIP_FREQ_SPREAD_MHZ": 50}},
        }
        cfg_path = self._save()
        with open(cfg_path) as f:
            on_disk = json.load(f)

        self.assertEqual(on_disk["version"], 4)
        self.assertIn("miner_configs", on_disk)
        self.assertIn("aa:bb:cc:dd:ee:01", on_disk["miner_configs"])
        self.assertNotIn("10.0.0.5", on_disk["miner_configs"])

    def test_save_load_roundtrip_v4(self):
        """A v4 in-memory state saves and re-loads with the same MAC-keyed shape."""
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"] = {
            "ip": "10.0.0.5",
            "current_firmware": "epic",
            "id_synthesized": False,
            "PASSWORD": "pw123",
            "platforms": {"epic": {"CHIP_FREQ_SPREAD_MHZ": 55}},
        }
        # Mark this IP as registered so orphan-cleanup on reload doesn't
        # purge the MAC entry whose ip field is "10.0.0.5".
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ["10.0.0.5"]
        cfg_path = self._save()

        # Wipe and reload
        state.MINER_CONFIGS.clear()
        apply_defaults()

        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("done")

        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(
                patch("tuner_app.config.persistence.resolve_mac", return_value="never-called")
            )
            stack.enter_context(
                patch("tuner_app.config.persistence.synthesize_mac_id", return_value="never-synth")
            )
            persistence.load_config_from_disk()

        self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertEqual(entry["current_firmware"], "epic")
        self.assertFalse(entry["id_synthesized"])
        self.assertEqual(entry["PASSWORD"], "pw123")
        self.assertIn("platforms", entry)
        self.assertEqual(entry["platforms"]["epic"]["CHIP_FREQ_SPREAD_MHZ"], 55)


class TestV1ToV4MigrationChain(unittest.TestCase):
    """v1 → v2 → v3 → v4 migration chain completes without error."""

    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_v1_to_v3_to_v4_chain(self):
        """Loading a v1 flat-dict config cascades through all migrations; defaults are populated."""
        v1_payload = {"BOARD_MAX_TEMP": 82, "SCAN_INTERVAL_MIN": 10}
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(v1_payload, f)

        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(
                patch("tuner_app.config.persistence.resolve_mac", return_value=None)
            )
            stack.enter_context(
                patch("tuner_app.config.persistence.synthesize_mac_id", return_value="syn-never")
            )
            persistence.load_config_from_disk()

        # v3 defaults are populated from the v1 values
        self.assertEqual(state.CONFIG["defaults"]["epic"]["BOARD_MAX_TEMP"], 82)
        self.assertEqual(state.CONFIG["fleet_ops"]["SCAN_INTERVAL_MIN"], 10)
        # New post-bug-B-fix spec: sentinel is written ONLY when migration actually re-keyed
        # entries. v1 has no miner_configs → nothing to re-key → no save → no sentinel.
        # The "no crash" check is the original key assertion of this test; it still holds.
        self.assertFalse(os.path.exists(sentinel))
        # MINER_CONFIGS is empty (v1 files have no per-miner data typically)
        # — no crash is the key assertion here
        # (cleanup may prune an empty MINER_CONFIGS with no MINER_IPS entries)


class TestV3ToV4VendorMacFetch(unittest.TestCase):
    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_happy_path_epic_vendor_api_returns_real_mac_no_fallback_calls(self):
        cfg_path = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        with ExitStack() as stack:
            mock_epic = stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            mock_resolve = stack.enter_context(patch("tuner_app.config.persistence.resolve_mac"))
            mock_synth = stack.enter_context(
                patch("tuner_app.config.persistence.synthesize_mac_id")
            )
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))

            mock_epic.return_value.summary.return_value = MagicMock(mac="aa:bb:cc:dd:ee:01")
            mock_resolve.return_value = "should-not-be-used"
            mock_synth.return_value = "syn-should-not-be-used"

            persistence.load_config_from_disk()

        self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
        self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertEqual(entry["current_firmware"], "epic")
        self.assertFalse(entry["id_synthesized"])
        mock_resolve.assert_not_called()
        mock_synth.assert_not_called()

    def test_fallback_chain_epic_api_raises_then_arp_returns_real_mac(self):
        cfg_path = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        with ExitStack() as stack:
            mock_epic = stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            mock_resolve = stack.enter_context(patch("tuner_app.config.persistence.resolve_mac"))
            mock_synth = stack.enter_context(
                patch("tuner_app.config.persistence.synthesize_mac_id")
            )
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))

            mock_epic.side_effect = RuntimeError("network down")
            mock_resolve.return_value = "11:22:33:44:55:66"
            mock_synth.return_value = "syn-never"

            persistence.load_config_from_disk()

        self.assertIn("11:22:33:44:55:66", state.MINER_CONFIGS)
        self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS["11:22:33:44:55:66"]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertEqual(entry["current_firmware"], "epic")
        self.assertFalse(entry["id_synthesized"])
        mock_synth.assert_not_called()

    def test_fallback_chain_epic_returns_none_arp_returns_none_then_synth(self):
        cfg_path = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        with ExitStack() as stack:
            mock_epic = stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            mock_resolve = stack.enter_context(patch("tuner_app.config.persistence.resolve_mac"))
            mock_synth = stack.enter_context(
                patch("tuner_app.config.persistence.synthesize_mac_id")
            )
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))

            mock_epic.return_value.summary.return_value = MagicMock(mac=None)
            mock_resolve.return_value = None
            mock_synth.return_value = "syn-10-0-0-5-deadbeef"

            persistence.load_config_from_disk()

        self.assertIn("syn-10-0-0-5-deadbeef", state.MINER_CONFIGS)
        self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS["syn-10-0-0-5-deadbeef"]
        self.assertEqual(entry["ip"], "10.0.0.5")
        self.assertEqual(entry["current_firmware"], "epic")
        self.assertTrue(entry["id_synthesized"])

    def test_non_epic_firmware_skips_vendor_client(self):
        for fw_type in ["bixbit", "luxos", "braiins"]:
            with self.subTest(firmware=fw_type):
                # Each subTest reuses self._tmp; remove the migration sentinel
                # written by the previous iteration so the migration runs again.
                sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
                if os.path.exists(sentinel):
                    os.remove(sentinel)
                cfg_path = _write_v3_config(
                    self._tmp,
                    fleet_ops={"MINER_IPS": ["10.0.0.5"]},
                    miner_configs={"10.0.0.5": {"firmware_type": fw_type}},
                )
                state.MINER_CONFIGS.clear()
                with ExitStack() as stack:
                    mock_epic = stack.enter_context(
                        patch("tuner_app.config.persistence.EpicMinerAPI")
                    )
                    mock_resolve = stack.enter_context(
                        patch("tuner_app.config.persistence.resolve_mac")
                    )
                    stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
                    stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
                    stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))

                    mock_resolve.return_value = "aa:bb:cc:dd:ee:01"

                    persistence.load_config_from_disk()

                self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
                self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
                mock_epic.assert_not_called()

    def test_helper_returns_none_when_constructor_raises(self):
        with patch("tuner_app.config.persistence.EpicMinerAPI") as mock_epic:
            mock_epic.side_effect = RuntimeError("network down")
            result = persistence._fetch_vendor_mac_for_v3_migration(
                "10.0.0.5", "epic", 4028, "letmein"
            )
            self.assertIsNone(result)

    def test_helper_returns_none_when_summary_raises(self):
        with patch("tuner_app.config.persistence.EpicMinerAPI") as mock_epic:
            mock_epic.return_value.summary.side_effect = RuntimeError("API error")
            result = persistence._fetch_vendor_mac_for_v3_migration(
                "10.0.0.5", "epic", 4028, "letmein"
            )
            self.assertIsNone(result)

    def test_helper_returns_none_when_summary_mac_is_none(self):
        with patch("tuner_app.config.persistence.EpicMinerAPI") as mock_epic:
            mock_epic.return_value.summary.return_value = MagicMock(mac=None)
            result = persistence._fetch_vendor_mac_for_v3_migration(
                "10.0.0.5", "epic", 4028, "letmein"
            )
            self.assertIsNone(result)

    def test_helper_returns_none_when_summary_mac_is_empty_string(self):
        with patch("tuner_app.config.persistence.EpicMinerAPI") as mock_epic:
            mock_epic.return_value.summary.return_value = MagicMock(mac="")
            result = persistence._fetch_vendor_mac_for_v3_migration(
                "10.0.0.5", "epic", 4028, "letmein"
            )
            self.assertIsNone(result)

    def test_helper_returns_none_for_non_epic_firmware(self):
        for fw_type in ["bixbit", "luxos", "braiins", "unknown"]:
            with (
                self.subTest(firmware=fw_type),
                patch("tuner_app.config.persistence.EpicMinerAPI") as mock_epic,
            ):  # noqa: SIM117 — py3.8 target rejects parenthesized `with`
                result = persistence._fetch_vendor_mac_for_v3_migration(
                    "10.0.0.5", fw_type, 4028, "letmein"
                )
                self.assertIsNone(result)
                mock_epic.assert_not_called()

    def test_helper_passes_correct_args_to_epic_api(self):
        with patch("tuner_app.config.persistence.EpicMinerAPI") as mock_epic:
            persistence._fetch_vendor_mac_for_v3_migration("192.168.1.1", "epic", 4028, "secretpw")
            mock_epic.assert_called_once_with("192.168.1.1", port=4028, password="secretpw")


class TestV3ToV4MigrationSentinelHonesty(unittest.TestCase):
    def setUp(self):
        self._saved_config = copy.deepcopy(state.CONFIG)
        self._saved_miner_configs = {k: dict(v) for k, v in state.MINER_CONFIGS.items()}
        apply_defaults()
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        state.CONFIG.clear()
        state.CONFIG.update(self._saved_config)
        state.MINER_CONFIGS.clear()
        for k, v in self._saved_miner_configs.items():
            state.MINER_CONFIGS[k] = v
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _patch_full_isolation(
        self, stack, cfg_path, resolve_rv=None, synth_rv="syn-default", epic_mac=None
    ):
        """Helper to install all 6 isolation patches. Returns dict of mock objects."""
        mocks = {}
        mocks["epic"] = stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
        mocks["resolve"] = stack.enter_context(patch("tuner_app.config.persistence.resolve_mac"))
        mocks["synth"] = stack.enter_context(
            patch("tuner_app.config.persistence.synthesize_mac_id")
        )
        stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", cfg_path))
        stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
        stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
        mocks["epic"].return_value.summary.return_value = MagicMock(mac=epic_mac)
        mocks["resolve"].return_value = resolve_rv
        mocks["synth"].return_value = synth_rv
        return mocks

    def test_sentinel_NOT_written_when_no_v3_entries_and_no_sentinel(self):
        cfg_path = _write_v3_config(self._tmp, miner_configs=None)
        with ExitStack() as stack:
            self._patch_full_isolation(stack, cfg_path)
            save_mock = stack.enter_context(
                patch("tuner_app.config.persistence.save_config_to_disk")
            )
            persistence.load_config_from_disk()
            sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
            self.assertFalse(os.path.exists(sentinel))
            save_mock.assert_not_called()

    def test_sentinel_written_after_migration_with_save_succeeding(self):
        cfg_path = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        with ExitStack() as stack:
            self._patch_full_isolation(stack, cfg_path, resolve_rv="aa:bb:cc:dd:ee:01")
            persistence.load_config_from_disk()
            sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
            self.assertTrue(os.path.exists(sentinel))
            self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)

    def test_sentinel_NOT_written_when_save_raises(self):
        cfg_path = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        with ExitStack() as stack:
            self._patch_full_isolation(stack, cfg_path, resolve_rv="aa:bb:cc:dd:ee:01")
            stack.enter_context(
                patch(
                    "tuner_app.config.persistence.save_config_to_disk",
                    side_effect=OSError("save failed"),
                )
            )
            persistence.load_config_from_disk()
            sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
            self.assertFalse(os.path.exists(sentinel))

    def test_self_heal_runs_migration_when_sentinel_exists_but_v3_entries_present(self):
        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("already done")
        cfg_path = _write_v3_config(
            self._tmp,
            fleet_ops={"MINER_IPS": ["10.0.0.5"]},
            miner_configs={"10.0.0.5": {"firmware_type": "epic"}},
        )
        with ExitStack() as stack:
            self._patch_full_isolation(stack, cfg_path, resolve_rv="aa:bb:cc:dd:ee:01")
            persistence.load_config_from_disk()
            self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)
            self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)

    def test_self_heal_skips_migration_when_sentinel_exists_and_only_v4_entries(self):
        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("already done")
        v4_payload = {
            "version": 4,
            "defaults": {p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
            "fleet_ops": {"MINER_IPS": ["10.0.0.5"]},
            "miner_configs": {
                "aa:bb:cc:dd:ee:01": {
                    "ip": "10.0.0.5",
                    "current_firmware": "epic",
                    "id_synthesized": False,
                    "platforms": {"epic": {}},
                }
            },
            "auth": {},
        }
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(v4_payload, f)
        with ExitStack() as stack:
            mocks = self._patch_full_isolation(stack, cfg_path)
            save_mock = stack.enter_context(
                patch("tuner_app.config.persistence.save_config_to_disk")
            )
            persistence.load_config_from_disk()
            mocks["resolve"].assert_not_called()
            mocks["synth"].assert_not_called()
            mocks["epic"].assert_not_called()
            save_mock.assert_not_called()

    def test_helper_returns_false_when_no_work_done(self):
        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("already done")
        state.MINER_CONFIGS.clear()
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            stack.enter_context(patch("tuner_app.config.persistence.resolve_mac"))
            stack.enter_context(patch("tuner_app.config.persistence.synthesize_mac_id"))
            result = persistence._maybe_run_v3_to_v4_migration()
            self.assertIs(result, False)

    def test_helper_returns_true_after_migrating_at_least_one_entry(self):
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS["10.0.0.5"] = {"firmware_type": "epic"}
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            stack.enter_context(
                patch("tuner_app.config.persistence.resolve_mac", return_value="aa:bb:cc:dd:ee:01")
            )
            stack.enter_context(patch("tuner_app.config.persistence.synthesize_mac_id"))
            result = persistence._maybe_run_v3_to_v4_migration()
            self.assertIs(result, True)
            self.assertIn("aa:bb:cc:dd:ee:01", state.MINER_CONFIGS)

    def test_helper_does_not_write_sentinel_directly_anymore(self):
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS["10.0.0.5"] = {"firmware_type": "epic"}
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            stack.enter_context(
                patch("tuner_app.config.persistence.resolve_mac", return_value="aa:bb:cc:dd:ee:01")
            )
            stack.enter_context(patch("tuner_app.config.persistence.synthesize_mac_id"))
            persistence._maybe_run_v3_to_v4_migration()
            sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
            self.assertFalse(os.path.exists(sentinel))

    def test_self_heal_uses_ipv4_regex_does_not_match_mac_or_synth_keys(self):
        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("already done")
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS["aa:bb:cc:dd:ee:01"] = {
            "ip": "10.0.0.1",
            "current_firmware": "epic",
            "platforms": {},
        }
        state.MINER_CONFIGS["syn-10-0-0-5-deadbeef"] = {
            "ip": "10.0.0.5",
            "current_firmware": "epic",
            "platforms": {},
        }
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            mock_resolve = stack.enter_context(patch("tuner_app.config.persistence.resolve_mac"))
            stack.enter_context(patch("tuner_app.config.persistence.synthesize_mac_id"))
            result = persistence._maybe_run_v3_to_v4_migration()
            self.assertIs(result, False)
            mock_resolve.assert_not_called()

    def test_self_heal_re_runs_for_partial_v3_state(self):
        sentinel = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel, "w") as f:
            f.write("already done")
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS["aa:bb:cc:dd:ee:99"] = {
            "ip": "10.0.0.99",
            "current_firmware": "epic",
            "platforms": {},
        }
        state.MINER_CONFIGS["10.0.0.5"] = {"firmware_type": "epic"}
        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            stack.enter_context(
                patch("tuner_app.config.persistence.resolve_mac", return_value="11:22:33:44:55:66")
            )
            stack.enter_context(patch("tuner_app.config.persistence.synthesize_mac_id"))
            result = persistence._maybe_run_v3_to_v4_migration()
            self.assertIs(result, True)
            self.assertIn("11:22:33:44:55:66", state.MINER_CONFIGS)
            self.assertNotIn("10.0.0.5", state.MINER_CONFIGS)
            self.assertIn("aa:bb:cc:dd:ee:99", state.MINER_CONFIGS)
