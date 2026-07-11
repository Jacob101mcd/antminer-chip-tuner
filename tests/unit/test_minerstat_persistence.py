"""Regression tests for minerstat snapshot persistence across restarts.

Bug history: during the modular restructure, `load_minerstat_snapshot()` was
moved from the legacy `tuner.py` (where it was called at module-load time)
into `tuner_app.profit.minerstat` but the call site was never re-wired into
`tuner_app.main.main()`. Effect: the on-disk snapshot file was written
correctly, but every process restart reset `state.MINERSTAT_SNAPSHOT` to {}
until the next manual fetch or scheduled poll fired. To the operator this
looked like minerstat data being cleared by every restart/update.
"""

import json
import os
import unittest
from unittest import mock

from tuner_app import state
from tuner_app.profit.minerstat import load_minerstat_snapshot


class TestLoadMinerstatSnapshot(unittest.TestCase):
    def setUp(self):
        # Snapshot the in-memory state and the constants module's MINERSTAT_FILE
        # so each test runs in isolation regardless of file order.
        self._saved_snapshot = dict(state.MINERSTAT_SNAPSHOT)
        state.MINERSTAT_SNAPSHOT.clear()

    def tearDown(self):
        state.MINERSTAT_SNAPSHOT.clear()
        state.MINERSTAT_SNAPSHOT.update(self._saved_snapshot)

    def _patch_file(self, path):
        # `load_minerstat_snapshot` reads MINERSTAT_FILE off the
        # `tuner_app.profit.minerstat` module, which imports it from
        # `tuner_app.constants`. Patch the binding the function actually
        # uses so we don't have to write to the real tuning_data/ dir.
        return mock.patch("tuner_app.profit.minerstat.MINERSTAT_FILE", path)

    def test_loads_persisted_snapshot_into_memory(self):
        """The whole point of the bug fix: a snapshot on disk gets rehydrated
        into state.MINERSTAT_SNAPSHOT at boot."""
        payload = {
            "captured_at": "2026-04-01T00:00:00Z",
            "last_poll_month": "2026-04",
            "api_calls_this_month": 3,
            "coins": {
                "BTC": {
                    "price_usd": 65000.0,
                    "reward_block": 3.125,
                    "network_hashrate": 6.0e20,
                    "block_time_s": 600,
                    "algorithm": "SHA-256",
                    "name": "Bitcoin",
                }
            },
        }
        with mock.patch("tuner_app.profit.minerstat.MINERSTAT_FILE") as mocked:
            tmp_path = os.path.join(os.path.dirname(__file__), "_minerstat_test.json")
            mocked.__str__ = lambda self: tmp_path  # cosmetic for repr; not load-bearing
            with open(tmp_path, "w") as f:
                json.dump(payload, f)
            try:
                with self._patch_file(tmp_path):
                    load_minerstat_snapshot()
                self.assertEqual(state.MINERSTAT_SNAPSHOT.get("api_calls_this_month"), 3)
                self.assertEqual(state.MINERSTAT_SNAPSHOT.get("last_poll_month"), "2026-04")
                self.assertIn("BTC", state.MINERSTAT_SNAPSHOT.get("coins", {}))
                self.assertEqual(state.MINERSTAT_SNAPSHOT["coins"]["BTC"]["price_usd"], 65000.0)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    def test_missing_file_is_silent_noop(self):
        """First-run case: no file on disk → snapshot stays empty, no exception."""
        tmp_path = os.path.join(os.path.dirname(__file__), "_does_not_exist.json")
        self.assertFalse(os.path.exists(tmp_path))
        with self._patch_file(tmp_path):
            load_minerstat_snapshot()  # must not raise
        self.assertEqual(dict(state.MINERSTAT_SNAPSHOT), {})

    def test_corrupted_json_is_silent_noop(self):
        """Half-written file or hand-edited garbage → no crash, snapshot
        stays empty so the operator can recover by clicking Fetch now."""
        tmp_path = os.path.join(os.path.dirname(__file__), "_minerstat_garbage.json")
        with open(tmp_path, "w") as f:
            f.write("{this is not valid json")
        try:
            with self._patch_file(tmp_path):
                load_minerstat_snapshot()  # must not raise
            self.assertEqual(dict(state.MINERSTAT_SNAPSHOT), {})
        finally:
            os.remove(tmp_path)

    def test_non_dict_payload_is_silent_noop(self):
        """If something writes a list/string instead of a dict, ignore it
        rather than poisoning the in-memory snapshot."""
        tmp_path = os.path.join(os.path.dirname(__file__), "_minerstat_list.json")
        with open(tmp_path, "w") as f:
            json.dump(["not", "a", "dict"], f)
        try:
            with self._patch_file(tmp_path):
                load_minerstat_snapshot()
            self.assertEqual(dict(state.MINERSTAT_SNAPSHOT), {})
        finally:
            os.remove(tmp_path)


class TestMainCallsLoadMinerstatSnapshot(unittest.TestCase):
    """Locks in the call-site invariant: `tuner_app.main.main()` MUST call
    load_minerstat_snapshot() at boot, otherwise the disk file never makes
    it back into memory and we regress to the original bug."""

    def test_main_function_calls_loader(self):
        import inspect

        import tuner_app.main as main_mod

        src = inspect.getsource(main_mod.main)
        self.assertIn(
            "load_minerstat_snapshot()",
            src,
            "tuner_app.main.main() must call load_minerstat_snapshot() — see "
            "regression note in this file's module docstring.",
        )


if __name__ == "__main__":
    unittest.main()
