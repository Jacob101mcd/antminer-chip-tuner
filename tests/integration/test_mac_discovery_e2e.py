"""End-to-end MAC discovery + v3→v4 migration smoke test.

Simulates a deployment recovery flow: a host with a v3 IP-keyed config,
a stale `.migration_v3_to_v4.done` sentinel, and per-miner tuning files. After
load_config_from_disk() runs, ePIC entries get re-keyed to canonical MACs via
the vendor /network endpoint (mocked at the miner_http_request boundary), the
Bixbit entry falls through to a synthesized ID, files are renamed, and the
on-disk config.json is rewritten as version 4.

Representative LUXminer ePIC `/summary`, `/network`, and `/capabilities`
response shapes use documentation-only identifiers and synthetic telemetry.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tuner_app import state
from tuner_app.config import persistence
from tuner_app.config.defaults import apply_defaults
from tuner_app.miner.types import MinerSummary

# Representative protocol fixtures with documentation-only addresses.
NETWORK_FIXTURE_A = {
    "dhcp": {
        "address": "192.0.2.10",
        "netmask": "255.255.255.0",
        "gateway": "192.0.2.1",
        "dns": "192.0.2.53",
        "dns2": "198.51.100.53",
        "dnsv6": None,
        "mac_address": "02:00:5E:10:00:01",
    }
}

NETWORK_FIXTURE_B = {
    "dhcp": {
        "address": "192.0.2.11",
        "netmask": "255.255.255.0",
        "gateway": "192.0.2.1",
        "dns": "192.0.2.53",
        "dns2": "198.51.100.53",
        "dnsv6": None,
        "mac_address": "aa:bb:cc:dd:ee:02",
    }
}

SUMMARY_FIXTURE = {
    "Status": {
        "Operating State": "Mining",
        "Last Command": None,
        "Last Command Result": None,
        "Last Error": None,
    },
    "Hostname": "miner-example",
    "HBs": [],
    "Fans": {
        "Fans Speed": 20,
        "Fan Mode": {"Manual": 20},
        "Minimum Working Fans": 0,
        "Working Fan Indices": [],
    },
    "Power Supply Stats": {
        "Input Voltage": 0.0,
        "Output Voltage": 14.0,
        "Input Current": 0.0,
        "Output Current": 0.0,
        "Input Power": 3000.0,
        "Output Power": 0.0,
        "Target Voltage": 14000,
    },
}

CAPS_FIXTURE = {
    "Model": "Antminer S21",
    "Model Subtype": "BHB68603",
    "Chip Type": "BM1368",
    "Chips Per Bank": 9,
}

# The synth ID format must match the synth-MAC regex `^syn-[0-9a-f][0-9a-f\-]*$`
# (see tuner_app.constants._SYNTH_MAC_RE). Real synth IDs from synthesize_mac_id
# are shaped `syn-{ip-with-dots-as-dashes}-{8 hex chars}`. Use a deterministic
# value here.
SYNTH_BIXBIT_ID = "syn-192-0-2-12-deadbeef"


class TestMacDiscoveryE2E(unittest.TestCase):
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
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_full_recovery_flow(self):
        """The full integration: three synthetic miners, stale sentinel,
        per-miner files. After load_config_from_disk(): MAC re-keying, file renames,
        on-disk v4 config, sentinel re-written."""
        # Pre-write config.json (v3 shape)
        config_path = os.path.join(self._tmp, "config.json")
        with open(config_path, "w") as f:
            json.dump(
                {
                    "version": 3,
                    "defaults": {p: {} for p in ("epic", "bixbit", "luxos", "braiins")},
                    "fleet_ops": {"MINER_IPS": ["192.0.2.10", "192.0.2.11", "192.0.2.12"]},
                    "miner_configs": {
                        "192.0.2.10": {"firmware_type": "epic", "PASSWORD": "letmein"},
                        "192.0.2.11": {"firmware_type": "epic"},
                        "192.0.2.12": {"firmware_type": "bixbit"},
                    },
                    "auth": {},
                },
                f,
            )

        # Pre-write stale sentinel
        sentinel_path = os.path.join(self._tmp, ".migration_v3_to_v4.done")
        with open(sentinel_path, "w") as f:
            f.write("stale")

        # Pre-write per-miner placeholder files
        for fname in (
            "192-0-2-10.json",
            "192-0-2-10.checkpoint.json",
            "192-0-2-10.stock.json",
            "192-0-2-10.log.jsonl",
            "192-0-2-11.checkpoint.json",
        ):
            with open(os.path.join(self._tmp, fname), "w") as f:
                json.dump({"phase": "test"}, f)

        # Mock dispatcher for the miner HTTP boundary
        def mock_miner_http_request(
            ip, port, path, data=None, method="GET", timeout=15, *, source_ip=None
        ):
            if path == "/summary":
                return (200, [], json.dumps(SUMMARY_FIXTURE).encode())
            if path == "/network":
                if ip == "192.0.2.10":
                    return (200, [], json.dumps(NETWORK_FIXTURE_A).encode())
                if ip == "192.0.2.11":
                    return (200, [], json.dumps(NETWORK_FIXTURE_B).encode())
                return (404, [], b"")
            if path == "/capabilities":
                return (200, [], json.dumps(CAPS_FIXTURE).encode())
            return (404, [], b"")

        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.CONFIG_FILE", config_path))
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            stack.enter_context(
                patch(
                    "tuner_app.miner.epic.miner_http_request", side_effect=mock_miner_http_request
                )
            )
            stack.enter_context(
                patch("tuner_app.config.persistence.resolve_mac", return_value=None)
            )
            stack.enter_context(
                patch(
                    "tuner_app.config.persistence.synthesize_mac_id", return_value=SYNTH_BIXBIT_ID
                )
            )
            persistence.load_config_from_disk()

        # ── State assertions ──────────────────────────────────────────────────
        self.assertEqual(len(state.MINER_CONFIGS), 3)
        self.assertIn("02:00:5e:10:00:01", state.MINER_CONFIGS)
        self.assertIn("aa:bb:cc:dd:ee:02", state.MINER_CONFIGS)
        self.assertIn(SYNTH_BIXBIT_ID, state.MINER_CONFIGS)

        self.assertNotIn("192.0.2.10", state.MINER_CONFIGS)
        self.assertNotIn("192.0.2.11", state.MINER_CONFIGS)
        self.assertNotIn("192.0.2.12", state.MINER_CONFIGS)

        miner1 = state.MINER_CONFIGS["02:00:5e:10:00:01"]
        self.assertEqual(miner1["ip"], "192.0.2.10")
        self.assertEqual(miner1["current_firmware"], "epic")
        self.assertFalse(miner1["id_synthesized"])
        self.assertEqual(miner1["PASSWORD"], "letmein")
        self.assertIn("platforms", miner1)
        self.assertIn("epic", miner1["platforms"])

        miner2 = state.MINER_CONFIGS["aa:bb:cc:dd:ee:02"]
        self.assertEqual(miner2["ip"], "192.0.2.11")
        self.assertEqual(miner2["current_firmware"], "epic")
        self.assertFalse(miner2["id_synthesized"])

        miner3 = state.MINER_CONFIGS[SYNTH_BIXBIT_ID]
        self.assertEqual(miner3["ip"], "192.0.2.12")
        self.assertEqual(miner3["current_firmware"], "bixbit")
        self.assertTrue(miner3["id_synthesized"])

        # ── On-disk v4 verification ──────────────────────────────────────────
        with open(config_path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["version"], 4)

        # ── File renames (new files exist, old ones absent) ──────────────────
        for new_name in (
            "02-00-5e-10-00-01.epic.profile.json",
            "02-00-5e-10-00-01.epic.checkpoint.json",
            "02-00-5e-10-00-01.epic.stock.json",
            "02-00-5e-10-00-01.log.jsonl",
        ):
            self.assertTrue(
                os.path.exists(os.path.join(self._tmp, new_name)),
                f"expected new file {new_name} to exist after migration",
            )

        for old_name in (
            "192-0-2-10.json",
            "192-0-2-10.checkpoint.json",
            "192-0-2-10.stock.json",
            "192-0-2-10.log.jsonl",
        ):
            self.assertFalse(
                os.path.exists(os.path.join(self._tmp, old_name)),
                f"expected old file {old_name} to be renamed away",
            )

        # ── Sentinel re-written after successful save ───────────────────────
        self.assertTrue(os.path.exists(sentinel_path))

    def test_canonical_mac_extraction_from_representative_network_json(self):
        """Narrow slice: MinerSummary.from_epic with representative /network JSON
        produces the canonical lowercase MAC. Useful for fast-diagnosis: if the big
        e2e test fails, this slice tells you whether the parser layer is to blame."""
        summary = MinerSummary.from_epic(SUMMARY_FIXTURE, raw_network=NETWORK_FIXTURE_A)
        self.assertEqual(summary.mac, "02:00:5e:10:00:01")

    def test_bixbit_without_vendor_api_falls_to_synth(self):
        """Narrow slice: Bixbit entry with ARP returning None falls through to synth.
        Verifies _fetch_vendor_mac_for_v3_migration short-circuits non-epic firmware
        without calling EpicMinerAPI."""
        # Direct in-memory state: Bixbit-only
        state.MINER_CONFIGS.clear()
        state.MINER_CONFIGS["192.0.2.12"] = {"firmware_type": "bixbit"}
        state.CONFIG["fleet_ops"]["MINER_IPS"] = ["192.0.2.12"]

        with ExitStack() as stack:
            stack.enter_context(patch("tuner_app.config.persistence.DATA_DIR", self._tmp))
            stack.enter_context(patch("tuner_app.constants.DATA_DIR", self._tmp))
            mock_epic = stack.enter_context(patch("tuner_app.config.persistence.EpicMinerAPI"))
            stack.enter_context(
                patch("tuner_app.config.persistence.resolve_mac", return_value=None)
            )
            stack.enter_context(
                patch(
                    "tuner_app.config.persistence.synthesize_mac_id", return_value=SYNTH_BIXBIT_ID
                )
            )
            result = persistence._maybe_run_v3_to_v4_migration()

        self.assertTrue(result, "migration should report a re-key happened")
        self.assertIn(SYNTH_BIXBIT_ID, state.MINER_CONFIGS)
        self.assertNotIn("192.0.2.12", state.MINER_CONFIGS)
        entry = state.MINER_CONFIGS[SYNTH_BIXBIT_ID]
        self.assertEqual(entry["ip"], "192.0.2.12")
        self.assertEqual(entry["current_firmware"], "bixbit")
        self.assertTrue(entry["id_synthesized"])
        # EpicMinerAPI must NOT be called for non-epic firmware
        mock_epic.assert_not_called()
