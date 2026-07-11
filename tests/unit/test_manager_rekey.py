"""Unit tests for tuner_app.manager.bulk._rekey_miner.

Tests FAIL on the current codebase because _rekey_miner does not exist yet.
Expected failure: ImportError / AttributeError on the import line.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.manager.bulk import _rekey_miner  # noqa: F401 — fails until implemented

_OLD_MAC = "aa:bb:cc:dd:ee:01"
_NEW_MAC = "aa:bb:cc:dd:ee:02"
_IP = "10.0.0.1"


def _v4_entry(ip=_IP, firmware="luxos", id_synthesized=True):
    return {
        "ip": ip,
        "current_firmware": firmware,
        "id_synthesized": id_synthesized,
        "platforms": {firmware: {"VOLTAGE_MV": 13500}},
    }


class _StubManager:
    def __init__(self):
        self.engines = {}
        self._lock = threading.RLock()

    def get_engine(self, identifier):
        return self.engines.get(identifier)

    def pop_engine(self, identifier):
        return self.engines.pop(identifier, None)


class TestRekeyHappyPath(unittest.TestCase):
    def setUp(self):
        apply_defaults()
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"].setdefault("MINER_IPS", [])

    def tearDown(self):
        state.MINER_CONFIGS.clear()

    def test_rekey_happy_path(self):
        state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()
        stub = _StubManager()

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("tuner_app.manager.bulk.save_config_to_disk") as mock_save,
            patch("tuner_app.manager.bulk.DATA_DIR", tmpdir),
        ):
            result = _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

        self.assertNotIn(_OLD_MAC, state.MINER_CONFIGS)
        self.assertIn(_NEW_MAC, state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[_NEW_MAC]
        self.assertFalse(entry["id_synthesized"])
        self.assertEqual(entry["current_firmware"], "luxos")
        self.assertEqual(entry["platforms"]["luxos"]["VOLTAGE_MV"], 13500)
        self.assertFalse(result["noop"])
        self.assertFalse(result["engine_rekeyed"])  # no engine in stub.engines
        self.assertEqual(result["renamed"], [])  # tmpdir is empty → no files matched
        mock_save.assert_called_once()

    def test_rekey_idempotent_when_old_mac_absent(self):
        stub = _StubManager()

        with patch("tuner_app.manager.bulk.save_config_to_disk") as mock_save:
            result = _rekey_miner("missing-mac", _NEW_MAC, manager=stub)

        self.assertEqual(result, {"renamed": [], "engine_rekeyed": False, "noop": True})
        mock_save.assert_not_called()

    def test_rekey_self_rekey_is_noop(self):
        state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()
        original_entry = dict(state.MINER_CONFIGS[_OLD_MAC])
        stub = _StubManager()

        with patch("tuner_app.manager.bulk.save_config_to_disk") as mock_save:
            result = _rekey_miner(_OLD_MAC, _OLD_MAC, manager=stub)

        self.assertEqual(result, {"renamed": [], "engine_rekeyed": False, "noop": True})
        self.assertIn(_OLD_MAC, state.MINER_CONFIGS)
        self.assertEqual(
            state.MINER_CONFIGS[_OLD_MAC]["current_firmware"],
            original_entry["current_firmware"],
        )
        mock_save.assert_not_called()

    def test_rekey_raises_on_target_conflict(self):
        state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()
        state.MINER_CONFIGS[_NEW_MAC] = _v4_entry(ip="10.0.0.2", firmware="epic")
        original_old = dict(state.MINER_CONFIGS[_OLD_MAC])
        original_new = dict(state.MINER_CONFIGS[_NEW_MAC])
        stub = _StubManager()

        with (
            patch("tuner_app.manager.bulk.save_config_to_disk") as mock_save,
            self.assertRaises(ValueError) as ctx,
        ):
            _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

        self.assertIn("already exists", str(ctx.exception))
        # Both entries remain unchanged
        self.assertIn(_OLD_MAC, state.MINER_CONFIGS)
        self.assertIn(_NEW_MAC, state.MINER_CONFIGS)
        self.assertEqual(state.MINER_CONFIGS[_OLD_MAC], original_old)
        self.assertEqual(state.MINER_CONFIGS[_NEW_MAC], original_new)
        mock_save.assert_not_called()


class TestRekeyEngineRegistry(unittest.TestCase):
    def setUp(self):
        apply_defaults()
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"].setdefault("MINER_IPS", [])

    def tearDown(self):
        state.MINER_CONFIGS.clear()

    def test_rekey_moves_engine_in_manager_registry(self):
        state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()
        stub = _StubManager()
        engine = MagicMock(name="engine-old")
        stub.engines[_OLD_MAC] = engine

        with patch("tuner_app.manager.bulk.save_config_to_disk"):
            result = _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

        self.assertNotIn(_OLD_MAC, stub.engines)
        self.assertIn(_NEW_MAC, stub.engines)
        self.assertIs(stub.engines[_NEW_MAC], engine)
        self.assertEqual(engine.mac, _NEW_MAC)
        self.assertTrue(result["engine_rekeyed"])

    def test_rekey_no_engine_skips_engine_step(self):
        state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()
        stub = _StubManager()  # engines dict is empty

        with patch("tuner_app.manager.bulk.save_config_to_disk"):
            result = _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

        self.assertFalse(result["engine_rekeyed"])
        self.assertNotIn(_OLD_MAC, stub.engines)
        self.assertNotIn(_NEW_MAC, stub.engines)

    def test_rekey_id_synthesized_cleared(self):
        state.MINER_CONFIGS[_OLD_MAC] = _v4_entry(id_synthesized=True)
        stub = _StubManager()

        with patch("tuner_app.manager.bulk.save_config_to_disk"):
            _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

        self.assertFalse(state.MINER_CONFIGS[_NEW_MAC]["id_synthesized"])


class TestRekeyFileRename(unittest.TestCase):
    def setUp(self):
        apply_defaults()
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"].setdefault("MINER_IPS", [])

    def tearDown(self):
        state.MINER_CONFIGS.clear()

    def test_rekey_file_rename(self):
        from tuner_app.constants import _mac_for_filename

        old_dash = _mac_for_filename(_OLD_MAC)
        new_dash = _mac_for_filename(_NEW_MAC)

        with tempfile.TemporaryDirectory() as tmpdir:
            filenames = [
                f"{old_dash}.epic.profile.json",
                f"{old_dash}.checkpoint.json",
                f"{old_dash}.log.jsonl",
            ]
            for fname in filenames:
                open(os.path.join(tmpdir, fname), "w").close()

            state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()
            stub = _StubManager()

            with (
                patch("tuner_app.manager.bulk.save_config_to_disk"),
                patch("tuner_app.manager.bulk.DATA_DIR", tmpdir),
            ):
                result = _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

            # All three files should now exist under the new dash form
            for fname in filenames:
                old_path = os.path.join(tmpdir, fname)
                new_fname = new_dash + fname[len(old_dash) :]
                new_path = os.path.join(tmpdir, new_fname)
                self.assertFalse(os.path.exists(old_path), f"old file still present: {fname}")
                self.assertTrue(os.path.exists(new_path), f"new file missing: {new_fname}")

            self.assertEqual(len(result["renamed"]), 3)
            for fname in filenames:
                self.assertIn(fname, result["renamed"])

    def test_rekey_file_rename_failure_does_not_abort(self):
        from tuner_app.constants import _mac_for_filename

        old_dash = _mac_for_filename(_OLD_MAC)
        new_dash = _mac_for_filename(_NEW_MAC)

        with tempfile.TemporaryDirectory() as tmpdir:
            good_fname = f"{old_dash}.epic.profile.json"
            bad_fname = f"{old_dash}.checkpoint.json"
            for fname in (good_fname, bad_fname):
                open(os.path.join(tmpdir, fname), "w").close()

            state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()
            stub = _StubManager()

            real_replace = os.replace

            def failing_replace(src, dst):
                if bad_fname in src:
                    raise OSError("simulated rename failure")
                return real_replace(src, dst)

            with (
                patch("tuner_app.manager.bulk.save_config_to_disk"),
                patch("tuner_app.manager.bulk.DATA_DIR", tmpdir),
                patch("os.replace", side_effect=failing_replace),
            ):
                result = _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

            # The successful rename should be in the result list
            self.assertIn(good_fname, result["renamed"])
            # The failed rename should NOT be in the list
            self.assertNotIn(bad_fname, result["renamed"])

            # The good file should have moved
            self.assertTrue(
                os.path.exists(os.path.join(tmpdir, new_dash + good_fname[len(old_dash) :])),
                "successfully renamed file should be present at new path",
            )
            # Config state still updated (rename failure is non-fatal)
            self.assertIn(_NEW_MAC, state.MINER_CONFIGS)
            self.assertNotIn(_OLD_MAC, state.MINER_CONFIGS)


class TestRekeyLockOrdering(unittest.TestCase):
    """state.config_lock must be released before engine work to avoid lock inversion."""

    def setUp(self):
        apply_defaults()
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"].setdefault("MINER_IPS", [])

    def tearDown(self):
        state.MINER_CONFIGS.clear()

    def test_rekey_releases_config_lock_before_engine_work(self):
        """state.config_lock is free when manager.engines[new_mac] is assigned.

        state.config_lock is a plain threading.Lock (non-reentrant), so
        acquire(blocking=False) from the same thread returns False if held.
        _RecordingDict snapshots that probe at every __setitem__ call.
        """
        state.MINER_CONFIGS[_OLD_MAC] = _v4_entry()

        config_lock_held_at_engine_insert: list[bool] = []

        class _RecordingDict(dict):
            def __setitem__(self, key, value):
                acquired = state.config_lock.acquire(blocking=False)
                if acquired:
                    state.config_lock.release()
                    config_lock_held_at_engine_insert.append(False)
                else:
                    config_lock_held_at_engine_insert.append(True)
                super().__setitem__(key, value)

        stub = _StubManager()
        stub.engines = _RecordingDict()
        stub.engines[_OLD_MAC] = MagicMock(name="engine-old")
        config_lock_held_at_engine_insert.clear()  # discard the setup insert

        with (
            patch("tuner_app.manager.bulk.save_config_to_disk"),
            patch("tuner_app.manager.bulk.DATA_DIR", tempfile.mkdtemp()),
        ):
            _rekey_miner(_OLD_MAC, _NEW_MAC, manager=stub)

        self.assertGreater(
            len(config_lock_held_at_engine_insert), 0, "engine re-key __setitem__ was never called"
        )
        self.assertFalse(
            config_lock_held_at_engine_insert[-1],
            "state.config_lock must be released before manager.engines[new_mac] is set",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
