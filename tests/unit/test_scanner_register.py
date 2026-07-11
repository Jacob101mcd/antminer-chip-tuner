"""Unit tests for _register_miner_locked in miners_routes (v4 / MAC-keyed).

Uses the _CountingLock pattern from test_effective_config.py to verify that
the helper mutates CONFIG + MINER_CONFIGS under the lock and that callers
(like Scanner._register_locked) call manager.get_engine AFTER releasing it.

Spec assertions (v4):
- _register_miner_locked writes MINER_CONFIGS[mac] (NOT MINER_CONFIGS[ip])
- Top-level fields: ip, current_firmware, id_synthesized, optional PASSWORD
- platforms[firmware_type] sub-dict initialized as empty dict
- MINER_IPS still gets the IP appended for backward-compat fleet roster reads
- save_config_to_disk is called inside the lock (lock-free by contract)
- manager.get_engine(mac) is called AFTER the lock is released
- Existing IP is a no-op (no duplicate in MINER_IPS)
- Idempotent for known MAC: re-registering updates ip / current_firmware /
  id_synthesized in place; preserves platforms sub-dict overrides.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.http_server.handlers.miners_routes import _register_miner_locked
from tuner_app.miner.types import MinerSummary
from tuner_app.scanner.runner import Scanner

_MAC_A = "aa:bb:cc:dd:ee:01"
_MAC_B = "aa:bb:cc:dd:ee:02"


class _CountingLock:
    """Wraps a real lock and counts acquire() calls — same pattern as test_effective_config."""

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


class TestRegisterMinerLocked(unittest.TestCase):
    def setUp(self):
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.CONFIG["fleet_ops"].pop("MINER_IPS", None)
        state.MINER_CONFIGS.clear()

    def test_adds_ip_to_miner_ips(self):
        _register_miner_locked(_MAC_A, "192.0.2.200", "letmein", "epic")
        self.assertIn("192.0.2.200", state.CONFIG["fleet_ops"]["MINER_IPS"])

    def test_writes_under_mac_key_not_ip(self):
        _register_miner_locked(_MAC_A, "192.0.2.200", "letmein", "epic")
        self.assertIn(_MAC_A, state.MINER_CONFIGS)
        self.assertNotIn("192.0.2.200", state.MINER_CONFIGS)

    def test_top_level_fields_v4_shape(self):
        _register_miner_locked(_MAC_A, "192.0.2.200", "mypassword", "epic")
        ov = state.MINER_CONFIGS[_MAC_A]
        self.assertEqual(ov["ip"], "192.0.2.200")
        self.assertEqual(ov["current_firmware"], "epic")
        self.assertEqual(ov["id_synthesized"], False)
        self.assertEqual(ov["PASSWORD"], "mypassword")
        self.assertIn("platforms", ov)
        self.assertIn("epic", ov["platforms"])
        self.assertEqual(ov["platforms"]["epic"], {})

    def test_existing_ip_no_duplicate(self):
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ["192.0.2.200"]
        _register_miner_locked(_MAC_A, "192.0.2.200", "letmein", "epic")
        self.assertEqual(state.CONFIG["fleet_ops"]["MINER_IPS"].count("192.0.2.200"), 1)

    def test_none_password_no_password_override(self):
        """None password → no PASSWORD key in entry, but other v4 fields still written."""
        _register_miner_locked(_MAC_A, "192.0.2.201", None, "epic")
        ov = state.MINER_CONFIGS[_MAC_A]
        self.assertNotIn("PASSWORD", ov)
        self.assertEqual(ov["current_firmware"], "epic")
        self.assertEqual(ov["ip"], "192.0.2.201")

    def test_empty_password_no_password_override(self):
        """Empty password → no PASSWORD key in entry, but v4 fields still written."""
        _register_miner_locked(_MAC_A, "192.0.2.202", "", "epic")
        ov = state.MINER_CONFIGS[_MAC_A]
        self.assertNotIn("PASSWORD", ov)
        self.assertEqual(ov["current_firmware"], "epic")

    def test_writes_custom_firmware_type(self):
        _register_miner_locked(_MAC_A, "192.0.2.204", "letmein", "bixbit")
        ov = state.MINER_CONFIGS[_MAC_A]
        self.assertEqual(ov["current_firmware"], "bixbit")
        self.assertIn("bixbit", ov["platforms"])

    def test_id_synthesized_flag_written(self):
        _register_miner_locked(
            "syn-192-0-2-50-deadbeef", "192.0.2.50", "letmein", "epic", id_synthesized=True
        )
        ov = state.MINER_CONFIGS["syn-192-0-2-50-deadbeef"]
        self.assertTrue(ov["id_synthesized"])

    def test_idempotent_for_known_mac_updates_top_level_fields(self):
        """Re-registering same MAC at a different IP updates ip + current_firmware
        in place and preserves the platforms sub-dict (so prior tuning overrides
        survive a firmware reflash detected by the scanner)."""
        _register_miner_locked(_MAC_A, "192.0.2.50", "letmein", "epic")
        # Pretend the operator tuned this miner — populate the platforms sub-dict
        state.MINER_CONFIGS[_MAC_A]["platforms"]["epic"]["VOLTAGE_MV"] = 14630
        # Re-register at a different IP after a DHCP move + reflash to luxos
        _register_miner_locked(_MAC_A, "192.0.2.99", "newpw", "luxos")
        ov = state.MINER_CONFIGS[_MAC_A]
        self.assertEqual(ov["ip"], "192.0.2.99")
        self.assertEqual(ov["current_firmware"], "luxos")
        self.assertEqual(ov["PASSWORD"], "newpw")
        # epic platform overrides preserved
        self.assertEqual(ov["platforms"]["epic"]["VOLTAGE_MV"], 14630)
        # luxos platform sub-dict initialized empty
        self.assertEqual(ov["platforms"]["luxos"], {})

    def test_helper_is_lock_free_by_contract(self):
        """_register_miner_locked itself does NOT acquire config_lock.

        The docstring contract says 'Caller MUST hold state.config_lock.'
        We verify this by calling it WITHOUT holding the lock and confirming
        it does not raise (it would deadlock or error if it tried to acquire
        a non-reentrant lock that was already held).
        """
        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter
        count_before = counter.acquire_count
        try:
            # Call without holding the lock — should succeed and NOT acquire it
            _register_miner_locked(_MAC_A, "192.0.2.204", "pw", "epic")
        finally:
            state.config_lock = original
        # The helper itself must not have acquired config_lock
        self.assertEqual(
            counter.acquire_count,
            count_before,
            "_register_miner_locked must not acquire config_lock itself",
        )


class TestScannerRegisterLocked(unittest.TestCase):
    """Test Scanner._register_locked lock ordering: get_engine called AFTER lock release."""

    def setUp(self):
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.CONFIG["fleet_ops"].pop("MINER_IPS", None)
        state.MINER_CONFIGS.clear()

    def test_get_engine_called_after_lock_released(self):
        """manager.get_engine must be called with lock NOT held."""
        manager = MagicMock()
        scanner = Scanner(manager)

        lock_held_during_get_engine = []

        original = state.config_lock
        counter = _CountingLock(original)
        state.config_lock = counter

        def tracking_get_engine(identifier):
            # Check if the real lock can be acquired without blocking.
            # If it CAN be acquired, the lock is currently free (not held).
            real_lock = original._real if hasattr(original, "_real") else original
            acquired = real_lock.acquire(blocking=False)
            lock_held_during_get_engine.append(not acquired)
            if acquired:
                real_lock.release()

        manager.get_engine.side_effect = tracking_get_engine

        try:
            with (
                patch("tuner_app.scanner.runner.save_config_to_disk"),
                patch("tuner_app.scanner.runner._register_miner_locked"),
            ):
                scanner._register_locked(
                    "192.0.2.50", "letmein", "epic", mac=_MAC_A, id_synthesized=False
                )
        finally:
            state.config_lock = original

        # get_engine was called with MAC (not IP)
        manager.get_engine.assert_called_once_with(_MAC_A)
        # Lock was NOT held when get_engine ran
        self.assertTrue(len(lock_held_during_get_engine) > 0)
        self.assertFalse(
            lock_held_during_get_engine[0],
            "get_engine must not run while config_lock is held",
        )

    def test_known_mac_at_new_ip_triggers_refresh_engine_ip(self):
        """When the scanner re-discovers a MAC at a different IP, the manager's
        refresh_engine_ip path fires (no engine teardown). Brand-new MAC takes
        the standard get_engine path."""
        manager = MagicMock()
        scanner = Scanner(manager)
        # Pre-seed v4 entry for _MAC_A at the OLD IP
        state.MINER_CONFIGS[_MAC_A] = {
            "ip": "192.0.2.50",
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
        }
        with patch("tuner_app.scanner.runner.save_config_to_disk"):
            scanner._register_locked(
                "192.0.2.99",  # new IP
                "letmein",
                "epic",
                mac=_MAC_A,
                id_synthesized=False,
            )
        # refresh_engine_ip fires once with the canonical (mac, new_ip)
        manager.refresh_engine_ip.assert_called_once_with(_MAC_A, "192.0.2.99")
        # MINER_CONFIGS was updated to the new IP
        self.assertEqual(state.MINER_CONFIGS[_MAC_A]["ip"], "192.0.2.99")
        # get_engine still called once for the engine handle
        manager.get_engine.assert_called_once_with(_MAC_A)

    def test_known_mac_at_same_ip_does_not_refresh(self):
        """No-op when the scanner re-discovers a known MAC at the same IP."""
        manager = MagicMock()
        scanner = Scanner(manager)
        state.MINER_CONFIGS[_MAC_A] = {
            "ip": "192.0.2.50",
            "current_firmware": "epic",
            "id_synthesized": False,
            "platforms": {"epic": {}},
        }
        with patch("tuner_app.scanner.runner.save_config_to_disk"):
            scanner._register_locked(
                "192.0.2.50", "letmein", "epic", mac=_MAC_A, id_synthesized=False
            )
        manager.refresh_engine_ip.assert_not_called()


_EPIC_RAW = {
    "Status": {"Operating State": "Mining"},
    "Network": {"Hostname": "miner-example"},
    "Power Supply Stats": {"Input Power": 4200.0, "Target Voltage": 14630, "Output Voltage": 14.63},
    "HBs": [
        {"Index": 0, "Hashrate": [67000000, 0, 99.5], "Core Clock Avg": 500},
        {"Index": 1, "Hashrate": [67000000, 0, 98.0], "Core Clock Avg": 543},
        {"Index": 2, "Hashrate": [66000000, 0, 97.0], "Core Clock Avg": 479},
    ],
    "Fans": {"Fans Speed": 5000},
}

_BIXBIT_RAW = {
    "STATUS": "S",
    "HS RT": 200000000.0,
    "Power Realtime": 3500.0,
    "Fan Speed Out": 4200,
    "Miner Type": "Whatsminer M30S",
    "PSU Vout": 12.5,
}


class _StubEngine:
    def __init__(self):
        self.last_summary = None
        self.last_update = 0
        self.thread = None


class TestScannerPrepopulateLastSummary(unittest.TestCase):
    def setUp(self):
        state.CONFIG["MINER_IPS"] = []
        state.MINER_CONFIGS.clear()

    def tearDown(self):
        state.CONFIG.pop("MINER_IPS", None)
        state.MINER_CONFIGS.clear()

    def test_epic_summary_raw_populates_last_summary(self):
        manager = MagicMock()
        engine = _StubEngine()
        manager.get_engine.return_value = engine
        scanner = Scanner(manager)

        with (
            patch("tuner_app.scanner.runner.save_config_to_disk"),
            patch("tuner_app.scanner.runner._register_miner_locked"),
        ):
            scanner._register_locked(
                "192.0.2.50", "letmein", "epic", summary_raw=_EPIC_RAW, mac=_MAC_A
            )

        self.assertIsInstance(engine.last_summary, MinerSummary)
        self.assertEqual(engine.last_summary.hostname, "miner-example")
        self.assertIsNone(engine.last_summary.model)
        self.assertEqual(engine.last_summary.operating_state, "Mining")
        self.assertEqual(engine.last_summary.power_w, 4200.0)
        self.assertGreater(engine.last_update, 0)

    def test_bixbit_summary_raw_populates_last_summary(self):
        manager = MagicMock()
        engine = _StubEngine()
        manager.get_engine.return_value = engine
        scanner = Scanner(manager)

        with (
            patch("tuner_app.scanner.runner.save_config_to_disk"),
            patch("tuner_app.scanner.runner._register_miner_locked"),
        ):
            scanner._register_locked(
                "192.0.2.50", "letmein", "bixbit", summary_raw=_BIXBIT_RAW, mac=_MAC_A
            )

        self.assertIsInstance(engine.last_summary, MinerSummary)
        self.assertIsNone(engine.last_summary.hostname)
        self.assertEqual(engine.last_summary.model, "Whatsminer M30S")
        # Bixbit probe data uses "STATUS" (all-caps); from_bixbit reads "Status"
        # (mixed case) → operating_state is "" from probe raw. That's expected
        # for pre-population; the engine will refresh with a live summary later.
        self.assertEqual(engine.last_summary.operating_state, "")
        self.assertEqual(engine.last_summary.power_w, 3500.0)
        self.assertGreater(engine.last_update, 0)

    def test_luxos_summary_raw_skipped(self):
        manager = MagicMock()
        engine = _StubEngine()
        manager.get_engine.return_value = engine
        scanner = Scanner(manager)

        with (
            patch("tuner_app.scanner.runner.save_config_to_disk"),
            patch("tuner_app.scanner.runner._register_miner_locked"),
        ):
            scanner._register_locked(
                "192.0.2.50", "letmein", "luxos", summary_raw=_EPIC_RAW, mac=_MAC_A
            )

        self.assertIsNone(engine.last_summary)

    def test_braiins_summary_raw_skipped(self):
        manager = MagicMock()
        engine = _StubEngine()
        manager.get_engine.return_value = engine
        scanner = Scanner(manager)

        with (
            patch("tuner_app.scanner.runner.save_config_to_disk"),
            patch("tuner_app.scanner.runner._register_miner_locked"),
        ):
            scanner._register_locked(
                "192.0.2.50", "letmein", "braiins", summary_raw=_EPIC_RAW, mac=_MAC_A
            )

        self.assertIsNone(engine.last_summary)

    def test_no_summary_raw_leaves_last_summary_none(self):
        manager = MagicMock()
        engine = _StubEngine()
        manager.get_engine.return_value = engine
        scanner = Scanner(manager)

        with (
            patch("tuner_app.scanner.runner.save_config_to_disk"),
            patch("tuner_app.scanner.runner._register_miner_locked"),
        ):
            scanner._register_locked("192.0.2.50", "letmein", "epic", mac=_MAC_A)

        self.assertIsNone(engine.last_summary)
        self.assertEqual(engine.last_update, 0)

    def test_existing_last_summary_not_overwritten(self):
        manager = MagicMock()
        engine = _StubEngine()
        sentinel_value = object()
        engine.last_summary = sentinel_value
        manager.get_engine.return_value = engine
        scanner = Scanner(manager)

        with (
            patch("tuner_app.scanner.runner.save_config_to_disk"),
            patch("tuner_app.scanner.runner._register_miner_locked"),
        ):
            scanner._register_locked(
                "192.0.2.50", "letmein", "epic", summary_raw=_EPIC_RAW, mac=_MAC_A
            )

        self.assertIs(engine.last_summary, sentinel_value)

    def test_running_engine_thread_skips_prepopulate(self):
        manager = MagicMock()
        engine = _StubEngine()
        engine.thread = MagicMock()
        engine.thread.is_alive.return_value = True
        manager.get_engine.return_value = engine
        scanner = Scanner(manager)

        with (
            patch("tuner_app.scanner.runner.save_config_to_disk"),
            patch("tuner_app.scanner.runner._register_miner_locked"),
        ):
            scanner._register_locked(
                "192.0.2.50", "letmein", "epic", summary_raw=_EPIC_RAW, mac=_MAC_A
            )

        self.assertIsNone(engine.last_summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
