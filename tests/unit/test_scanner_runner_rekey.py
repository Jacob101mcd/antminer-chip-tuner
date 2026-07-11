# tests/unit/test_scanner_runner_rekey.py
"""Unit tests for Unit 6 behaviors on Scanner._register_locked:

  Behavior A — Braiins registration-time MAC fetch via BraiinsMinerAPI.summary()
  Behavior B — Opportunistic synth-to-real re-key via _rekey_miner

These tests MUST FAIL on the current codebase because neither behavior is
implemented yet in tuner_app/scanner/runner.py.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from tuner_app import state
from tuner_app.config.defaults import apply_defaults
from tuner_app.miner.types import MinerSummary
from tuner_app.scanner.runner import Scanner

# ── Constants ────────────────────────────────────────────────────────────────

_IP = "10.0.0.1"
_OTHER_IP = "10.0.0.99"
_REAL_MAC_01 = "aa:bb:cc:dd:ee:01"
_REAL_MAC_02 = "aa:bb:cc:dd:ee:02"
_REAL_MAC_03 = "aa:bb:cc:dd:ee:03"
_SYNTH_MAC = "syn-0a000001"
_PASSWORD = "letmein"

# Patch targets
_PATCH_SAVE = "tuner_app.scanner.runner.save_config_to_disk"
_PATCH_BRAIINS = "tuner_app.scanner.runner.BraiinsMinerAPI"
_PATCH_REKEY = "tuner_app.scanner.runner._rekey_miner"


# ── Stub manager ─────────────────────────────────────────────────────────────


class _StubManager:
    """Minimal manager stub: records get_engine calls, keeps an engine dict."""

    def __init__(self):
        self.engines: dict = {}
        self._lock = threading.RLock()

    def get_engine(self, identifier):
        if identifier not in self.engines:
            eng = MagicMock(name=f"engine-{identifier}")
            eng.mac = identifier
            eng.last_summary = None
            eng.thread = None
            self.engines[identifier] = eng
        return self.engines[identifier]

    def peek_engine(self, identifier):
        return self.engines.get(identifier)

    def pop_engine(self, identifier):
        return self.engines.pop(identifier, None)

    def refresh_engine_ip(self, mac, new_ip):
        pass


# ── Base test case ────────────────────────────────────────────────────────────


class _Base(unittest.TestCase):
    def setUp(self):
        apply_defaults()
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []
        self.manager = _StubManager()
        self.scanner = Scanner(manager=self.manager)

    def tearDown(self):
        state.MINER_CONFIGS.clear()
        state.CONFIG["fleet_ops"]["MINER_IPS"] = []

    def _call_register(
        self,
        ip=_IP,
        password=_PASSWORD,
        firmware_type="braiins",
        mac=_SYNTH_MAC,
        id_synthesized=True,
        **kw,
    ):
        with patch(_PATCH_SAVE):
            self.scanner._register_locked(
                ip, password, firmware_type, mac=mac, id_synthesized=id_synthesized, **kw
            )


# ── Behavior A tests (Braiins MAC fetch) ─────────────────────────────────────


class TestBraiinsRegisterFetchesMacViaApi(_Base):
    """A-1: synth mac + braiins firmware → fetch real MAC from summary()."""

    def test_braiins_register_fetches_mac_via_api(self):
        real_summary = MinerSummary(
            operating_state="Mining",
            hashrate_ths=200.0,
            power_w=3500.0,
            fan_speed=3000,
            mac=_REAL_MAC_01,
        )

        with patch(_PATCH_BRAIINS) as mock_cls, patch(_PATCH_SAVE):
            mock_instance = MagicMock()
            mock_instance.summary.return_value = real_summary
            mock_cls.return_value = mock_instance

            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "braiins",
                mac=_SYNTH_MAC,
                id_synthesized=True,
            )

        # Real MAC entry must exist with id_synthesized=False
        self.assertIn(_REAL_MAC_01, state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[_REAL_MAC_01]
        self.assertFalse(
            entry.get("id_synthesized", True), "id_synthesized must be False when real MAC fetched"
        )

        # Synth key must be gone
        self.assertNotIn(_SYNTH_MAC, state.MINER_CONFIGS)


class TestBraiinsRegisterFallbackWhenApiFails(_Base):
    """A-2: BraiinsMinerAPI.summary() raises → preserve synth mac gracefully."""

    def test_braiins_register_falls_back_when_api_fails(self):
        with patch(_PATCH_BRAIINS) as mock_cls, patch(_PATCH_SAVE):
            mock_instance = MagicMock()
            mock_instance.summary.side_effect = ConnectionRefusedError("refused")
            mock_cls.return_value = mock_instance

            # Must NOT raise
            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "braiins",
                mac=_SYNTH_MAC,
                id_synthesized=True,
            )

        # Synth key must still be registered
        self.assertIn(_SYNTH_MAC, state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[_SYNTH_MAC]
        self.assertTrue(
            entry.get("id_synthesized", False), "id_synthesized must remain True on API failure"
        )


class TestBraiinsRegisterFallbackWhenSummaryMacIsNone(_Base):
    """A-3: summary().mac is None → preserve synth mac."""

    def test_braiins_register_falls_back_when_summary_mac_is_none(self):
        null_summary = MinerSummary(
            operating_state="Mining",
            hashrate_ths=200.0,
            power_w=3500.0,
            fan_speed=3000,
            mac=None,
        )

        with patch(_PATCH_BRAIINS) as mock_cls, patch(_PATCH_SAVE):
            mock_instance = MagicMock()
            mock_instance.summary.return_value = null_summary
            mock_cls.return_value = mock_instance

            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "braiins",
                mac=_SYNTH_MAC,
                id_synthesized=True,
            )

        self.assertIn(_SYNTH_MAC, state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[_SYNTH_MAC]
        self.assertTrue(
            entry.get("id_synthesized", False),
            "id_synthesized must remain True when summary mac is None",
        )


class TestBraiinsRegisterSkipsApiFetchWhenProbeMacIsReal(_Base):
    """A-4: probe already has a real mac → BraiinsMinerAPI must NOT be called."""

    def test_braiins_register_skips_api_fetch_when_probe_mac_is_real(self):
        with patch(_PATCH_BRAIINS) as mock_cls, patch(_PATCH_SAVE):
            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "braiins",
                mac=_REAL_MAC_02,
                id_synthesized=False,
            )

            mock_cls.assert_not_called()

        self.assertIn(_REAL_MAC_02, state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[_REAL_MAC_02]
        self.assertFalse(entry.get("id_synthesized", True))


# ── Behavior B tests (opportunistic synth-to-real re-key) ────────────────────


class TestOpportunisticRekeyMovesSynthEntryToRealMac(_Base):
    """B-5: a synth entry for the same IP exists → rekey it to the real MAC."""

    def setUp(self):
        super().setUp()
        # Pre-seed a synth entry with per-platform tuning data
        with state.config_lock:
            state.MINER_CONFIGS["syn-old"] = {
                "ip": _IP,
                "current_firmware": "luxos",
                "id_synthesized": True,
                "platforms": {"luxos": {"VOLTAGE_MV": 13500}},
            }

    def test_opportunistic_rekey_moves_synth_entry_to_real_mac(self):
        with patch(_PATCH_SAVE):
            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "luxos",
                mac=_REAL_MAC_03,
                id_synthesized=False,
            )

        # Synth entry must be gone
        self.assertNotIn(
            "syn-old", state.MINER_CONFIGS, "synth entry should have been re-keyed away"
        )

        # Real MAC entry must exist
        self.assertIn(_REAL_MAC_03, state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[_REAL_MAC_03]
        self.assertFalse(entry.get("id_synthesized", True))

        # Migrated platforms data must be preserved under the real MAC
        platforms = entry.get("platforms", {})
        luxos_overrides = platforms.get("luxos", {})
        self.assertEqual(
            luxos_overrides.get("VOLTAGE_MV"), 13500, "platforms tuning data must survive re-key"
        )


class TestNoRekeyWhenNoSynthEntryExists(_Base):
    """B-6: no synth entry → normal registration, no re-key attempted."""

    def test_no_rekey_when_no_synth_entry_exists(self):
        with patch(_PATCH_SAVE), patch(_PATCH_REKEY) as mock_rekey:
            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "epic",
                mac=_REAL_MAC_01,
                id_synthesized=False,
            )

            mock_rekey.assert_not_called()

        self.assertIn(_REAL_MAC_01, state.MINER_CONFIGS)


class TestNoRekeyWhenSynthEntryForDifferentIp(_Base):
    """B-7: synth entry for a different IP → must NOT be re-keyed."""

    def setUp(self):
        super().setUp()
        with state.config_lock:
            state.MINER_CONFIGS["syn-other"] = {
                "ip": _OTHER_IP,  # different IP
                "current_firmware": "epic",
                "id_synthesized": True,
                "platforms": {"epic": {}},
            }

    def test_no_rekey_when_synth_entry_for_different_ip(self):
        with patch(_PATCH_SAVE), patch(_PATCH_REKEY) as mock_rekey:
            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "epic",
                mac=_REAL_MAC_01,
                id_synthesized=False,
            )

            mock_rekey.assert_not_called()

        # Different-IP synth entry is untouched
        self.assertIn("syn-other", state.MINER_CONFIGS)
        self.assertEqual(state.MINER_CONFIGS["syn-other"]["ip"], _OTHER_IP)

        # New entry registered at real MAC
        self.assertIn(_REAL_MAC_01, state.MINER_CONFIGS)


class TestNoRekeyWhenProbeMacIsSynth(_Base):
    """B-8: probe returned a synth mac (non-Braiins) → no re-key attempted."""

    def test_no_rekey_when_probe_mac_is_synth(self):
        with patch(_PATCH_SAVE), patch(_PATCH_REKEY) as mock_rekey:
            self.scanner._register_locked(
                _IP,
                _PASSWORD,
                "luxos",
                mac=_SYNTH_MAC,
                id_synthesized=True,
            )

            mock_rekey.assert_not_called()

        self.assertIn(_SYNTH_MAC, state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[_SYNTH_MAC]
        self.assertTrue(
            entry.get("id_synthesized", False),
            "synth mac registration must preserve id_synthesized=True",
        )


if __name__ == "__main__":
    unittest.main()
