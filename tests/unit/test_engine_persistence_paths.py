"""A8: Engine takes MAC + per-platform persistence paths.

Verifies:
- engine.mac / engine.firmware_type set from constructor + config
- profile / checkpoint / stock baseline paths are per-platform
  (tuning_data/{mac-dashes}.{firmware}.{ext})
- log path is MAC-only (cross-platform): tuning_data/{mac-dashes}.log.jsonl
- log JSONL entries carry firmware_type field
- Legacy fallback when engine.mac fails MAC validation: per-platform helpers
  drop to the tolerant _miner_data_path shape so v3 fixture / IP-keyed test
  callers keep working through the PR3 transition.
- v4 entries with current_firmware="luxos" produce LuxOS-prefixed filenames;
  switching firmware_type produces a different filename for the same MAC.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections import deque
from unittest.mock import patch

from tuner_app import state
from tuner_app.constants import _mac_for_filename, _miner_data_path, _miner_platform_path

_MAC_A = "aa:bb:cc:dd:ee:01"
_MAC_DASHES = _mac_for_filename(_MAC_A)
_SYNTH_ID = "syn-192-0-2-50-deadbeef"


class _FakeConfig:
    """Minimal EffectiveConfig-like object for engine construction tests."""

    def __init__(self, overrides=None, ip=""):
        self._data = {
            "API_PORT": 4028,
            "PASSWORD": "letmein",
            "current_firmware": "epic",
            "LOG_DEDUP_WINDOW_SEC": 0,
        }
        if overrides:
            self._data.update(overrides)
        self._ip = ip

    @property
    def ip(self):
        return self._ip

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __contains__(self, key):
        return key in self._data


def _make_engine(mac, firmware="epic", ip="192.0.2.50"):
    from tuner_app.tuning_engine.engine import TuningEngine

    cfg = _FakeConfig({"current_firmware": firmware}, ip=ip)
    with (
        patch("tuner_app.tuning_engine.engine.persistence.restore_saved_state"),
        patch("tuner_app.tuning_engine.engine.logging_.load_log_from_disk"),
    ):
        return TuningEngine(mac, cfg)


class TestEngineMacAndFirmwareAttributes(unittest.TestCase):
    def test_mac_attribute_set_from_constructor(self):
        engine = _make_engine(_MAC_A, ip="192.0.2.50")
        self.assertEqual(engine.mac, _MAC_A)

    def test_firmware_type_attribute_from_current_firmware(self):
        engine = _make_engine(_MAC_A, firmware="luxos")
        self.assertEqual(engine.firmware_type, "luxos")

    def test_ip_attribute_from_config(self):
        engine = _make_engine(_MAC_A, ip="192.0.2.99")
        self.assertEqual(engine.ip, "192.0.2.99")

    def test_legacy_firmware_type_key_supported(self):
        """Pre-v4 fixtures using 'firmware_type' key still resolve correctly."""
        from tuner_app.tuning_engine.engine import TuningEngine

        cfg = _FakeConfig(ip="192.0.2.50")
        cfg._data.pop("current_firmware", None)
        cfg._data["firmware_type"] = "bixbit"
        with (
            patch("tuner_app.tuning_engine.engine.persistence.restore_saved_state"),
            patch("tuner_app.tuning_engine.engine.logging_.load_log_from_disk"),
        ):
            engine = TuningEngine(_MAC_A, cfg)
        self.assertEqual(engine.firmware_type, "bixbit")


class TestPerPlatformPersistencePaths(unittest.TestCase):
    def test_profile_path_includes_firmware(self):
        from tuner_app.tuning_engine import persistence

        engine = _make_engine(_MAC_A, firmware="epic")
        expected = _miner_platform_path(_MAC_A, "epic", ".profile.json")
        self.assertEqual(persistence.profile_path(engine), expected)

    def test_checkpoint_path_includes_firmware(self):
        from tuner_app.tuning_engine import persistence

        engine = _make_engine(_MAC_A, firmware="luxos")
        expected = _miner_platform_path(_MAC_A, "luxos", ".checkpoint.json")
        self.assertEqual(persistence.checkpoint_path(engine), expected)

    def test_stock_file_includes_firmware(self):
        from tuner_app.tuning_engine import persistence

        engine = _make_engine(_MAC_A, firmware="braiins")
        expected = _miner_platform_path(_MAC_A, "braiins", ".stock.json")
        self.assertEqual(persistence.stock_file(engine), expected)

    def test_filename_diverges_by_firmware_for_same_mac(self):
        """Reflashing a miner from epic to luxos produces a different on-disk
        path so the prior firmware's tuning data is preserved separately."""
        from tuner_app.tuning_engine import persistence

        engine_epic = _make_engine(_MAC_A, firmware="epic")
        engine_luxos = _make_engine(_MAC_A, firmware="luxos")
        self.assertNotEqual(
            persistence.profile_path(engine_epic), persistence.profile_path(engine_luxos)
        )
        # Both contain the same MAC dashes
        self.assertIn(_MAC_DASHES, persistence.profile_path(engine_epic))
        self.assertIn(_MAC_DASHES, persistence.profile_path(engine_luxos))


class TestSynthIdPersistencePaths(unittest.TestCase):
    """Synth IDs (e.g., L3-isolated miners) flow through the same per-platform
    helpers; they're already dash-form so _mac_for_filename passes them through."""

    def test_synth_profile_path_includes_firmware(self):
        from tuner_app.tuning_engine import persistence

        engine = _make_engine(_SYNTH_ID, firmware="epic")
        expected = _miner_platform_path(_SYNTH_ID, "epic", ".profile.json")
        self.assertEqual(persistence.profile_path(engine), expected)


class TestLegacyFallbackForInvalidMac(unittest.TestCase):
    """When engine.mac is an IP (test fixture / pre-migration), the per-platform
    helpers drop to legacy _miner_data_path naming so existing tests keep working."""

    def test_legacy_fallback_for_ip_keyed_engine(self):
        from tuner_app.tuning_engine import persistence

        engine = _make_engine("192.0.2.50", firmware="epic", ip="192.0.2.50")
        # IP fails _mac_for_filename validation → fallback to legacy _miner_data_path
        self.assertEqual(persistence.profile_path(engine), _miner_data_path("192.0.2.50", ".json"))
        self.assertEqual(
            persistence.checkpoint_path(engine),
            _miner_data_path("192.0.2.50", ".checkpoint.json"),
        )
        self.assertEqual(
            persistence.stock_file(engine), _miner_data_path("192.0.2.50", ".stock.json")
        )


class TestLogPathIsMacOnly(unittest.TestCase):
    """log file path uses _miner_data_path with engine.mac (cross-platform —
    survives reflash). No firmware suffix."""

    def test_log_path_uses_mac_no_firmware(self):
        from tuner_app.constants import DATA_DIR

        engine = _make_engine(_MAC_A, firmware="epic")
        expected = os.path.join(DATA_DIR, _MAC_DASHES + ".log.jsonl")
        # The log function builds path via _miner_data_path(engine.mac, ".log.jsonl")
        self.assertEqual(_miner_data_path(engine.mac, ".log.jsonl"), expected)

    def test_log_path_unchanged_across_firmware_swaps(self):
        """Reflashing produces the same log file path so the timeline is continuous."""
        engine_epic = _make_engine(_MAC_A, firmware="epic")
        engine_luxos = _make_engine(_MAC_A, firmware="luxos")
        self.assertEqual(
            _miner_data_path(engine_epic.mac, ".log.jsonl"),
            _miner_data_path(engine_luxos.mac, ".log.jsonl"),
        )


class TestLogEntriesCarryFirmwareType(unittest.TestCase):
    """Each JSONL log entry includes the engine's firmware_type so a reader
    can filter by post-flash firmware."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher = patch("tuner_app.constants.DATA_DIR", self._tmpdir.name)
        self._patcher.start()
        # Also patch the import in logging_ since it imports DATA_DIR via _miner_data_path
        self._patcher_log = patch(
            "tuner_app.tuning_engine.logging_._miner_data_path",
            side_effect=lambda mac, suffix: os.path.join(
                self._tmpdir.name, mac.replace(":", "-").replace(".", "-") + suffix
            ),
        )
        self._patcher_log.start()

    def tearDown(self):
        self._patcher_log.stop()
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_log_entry_includes_firmware_type(self):
        from tuner_app.tuning_engine.logging_ import log

        engine = _make_engine(_MAC_A, firmware="epic")
        # Engine constructor doesn't initialize log_lines via _make_engine path
        engine.log_lines = deque(maxlen=engine.LOG_LINES_MAX_CAP)
        engine._destroyed = False

        log(engine, "test message", level="INFO")

        # Read back the last in-memory entry
        self.assertEqual(len(engine.log_lines), 1)
        entry = engine.log_lines[-1]
        self.assertEqual(entry["firmware_type"], "epic")
        self.assertEqual(entry["msg"], "test message")

        # Check disk file contains the firmware_type
        log_file = os.path.join(self._tmpdir.name, _MAC_DASHES + ".log.jsonl")
        with open(log_file) as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 1)
        disk_entry = json.loads(lines[0])
        self.assertEqual(disk_entry["firmware_type"], "epic")

    def test_log_entry_firmware_type_reflects_engine(self):
        from tuner_app.tuning_engine.logging_ import log

        engine = _make_engine(_MAC_A, firmware="luxos")
        engine.log_lines = deque(maxlen=engine.LOG_LINES_MAX_CAP)
        engine._destroyed = False

        log(engine, "luxos line", level="WARN")
        self.assertEqual(engine.log_lines[-1]["firmware_type"], "luxos")
        self.assertEqual(engine.log_lines[-1]["level"], "WARN")


class TestSavedProfileCarriesMacAndFirmware(unittest.TestCase):
    """Saved profile/checkpoint payloads include mac + firmware_type fields
    so a reader can correlate the file to the canonical entry."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._patcher = patch("tuner_app.constants.DATA_DIR", self._tmpdir.name)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmpdir.cleanup()

    def test_save_profile_carries_mac_and_firmware(self):
        from tuner_app.tuning_engine.persistence import profile_path, save_profile

        engine = _make_engine(_MAC_A, firmware="luxos", ip="192.0.2.50")
        # Build a minimal but realistic engine state for save_profile to serialize
        engine._destroyed = False
        engine.log_lines = deque(maxlen=engine.LOG_LINES_MAX_CAP)

        # patch the path helper to use our tmpdir
        with patch(
            "tuner_app.tuning_engine.persistence._miner_platform_path",
            side_effect=lambda m, fw, suffix: os.path.join(
                self._tmpdir.name, m.replace(":", "-") + "." + fw + suffix
            ),
        ):
            save_profile(engine)
            path = profile_path(engine)
        with open(path) as f:
            payload = json.load(f)
        self.assertEqual(payload["mac"], _MAC_A)
        self.assertEqual(payload["firmware_type"], "luxos")
        self.assertEqual(payload["ip"], "192.0.2.50")

    def test_profile_and_checkpoint_strip_credentials_recursively(self):
        from tuner_app.tuning_engine.persistence import (
            checkpoint_path,
            profile_path,
            save_checkpoint,
            save_profile,
        )

        engine = _make_engine(_MAC_A, firmware="epic", ip="192.0.2.50")
        engine._destroyed = False
        engine.log_lines = deque(maxlen=engine.LOG_LINES_MAX_CAP)
        engine.config_snapshot = {
            "PASSWORD": "profile-password-value",
            "SCAN_PASSWORDS": ["profile-scan-value"],
            "MRR_API_KEY": "profile-mrr-key",
            "MRR_API_SECRET": "profile-mrr-secret",
            "MINERSTAT_API_KEY": "profile-minerstat-key",
            "SAFE_SETTING": 7,
        }
        engine.voltage_results = [{"voltage_mv": 12000, "password_hash": "nested-auth-hash"}]

        with patch(
            "tuner_app.tuning_engine.persistence._miner_platform_path",
            side_effect=lambda m, fw, suffix: os.path.join(
                self._tmpdir.name, m.replace(":", "-") + "." + fw + suffix
            ),
        ):
            save_profile(engine)
            profile = profile_path(engine)
            save_checkpoint(engine)
            checkpoint = checkpoint_path(engine)

        for path in (profile, checkpoint):
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            serialized = json.dumps(payload)
            for secret in (
                "profile-password-value",
                "profile-scan-value",
                "profile-mrr-key",
                "profile-mrr-secret",
                "profile-minerstat-key",
                "nested-auth-hash",
            ):
                self.assertNotIn(secret, serialized)
            self.assertEqual(payload["config_snapshot"]["SAFE_SETTING"], 7)


class TestEngineConstructorBackcompatWithIp(unittest.TestCase):
    """Pre-A9 manager call path: TuningEngine(ip, EffectiveConfig(ip)).
    Engine should still construct without error and use the IP as the
    canonical (legacy fallback) key."""

    def setUp(self):
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.MINER_CONFIGS.clear()

    def test_ip_constructor_back_compat(self):
        engine = _make_engine("192.0.2.50", firmware="epic", ip="192.0.2.50")
        self.assertEqual(engine.mac, "192.0.2.50")
        self.assertEqual(engine.ip, "192.0.2.50")
        self.assertEqual(engine.firmware_type, "epic")


if __name__ == "__main__":
    unittest.main(verbosity=2)
