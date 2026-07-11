"""Tests for _miner_data_path (colon-replace upgrade) and _miner_platform_path (new helper).

Spec references:
  - _miner_data_path: tuner_app/constants.py upgraded to replace BOTH '.' and ':'
    with '-' so MAC-formatted identifiers get filesystem-safe names.
  - _miner_platform_path: NEW helper, returns
    os.path.join(DATA_DIR, _mac_for_filename(mac) + '.' + firmware + suffix).
    Reuses _mac_for_filename (validates via _normalize_mac).
  - DATA_DIR: tuner_app/constants.py — ASIC_TUNER_DATA_DIR override or the
    platform-native user data directory.
"""

from __future__ import annotations

import os
from unittest import TestCase
from unittest.mock import patch

from tuner_app.constants import (
    DATA_DIR,
    DATA_DIR_ENV_VAR,
    _miner_data_path,
    _miner_platform_path,
    _resolve_data_dir,
)


class TestDataDirectoryResolution(TestCase):
    """Runtime state lives in an operator override or a platform data directory."""

    def test_environment_override_wins(self) -> None:
        override = os.path.join("relative", "operator-data")
        with patch.dict(os.environ, {DATA_DIR_ENV_VAR: override}):
            self.assertEqual(
                _resolve_data_dir(),
                os.path.abspath(os.path.expanduser(override)),
            )

    def test_platform_directory_is_default(self) -> None:
        expected = os.path.abspath(os.path.join("mock-platform-data", "antminer-chip-tuner"))
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("tuner_app.constants.user_data_path", return_value=expected) as user_data,
        ):
            self.assertEqual(_resolve_data_dir(), expected)

        user_data.assert_called_once_with("antminer-chip-tuner", appauthor=False)


class TestMinerDataPathIPInput(TestCase):
    """Case A: legacy IP input — dot-to-dash, unchanged by the upgrade."""

    def test_ip_dot_suffix(self) -> None:
        """_miner_data_path with IPv4 + .json suffix produces dashed-IP filename under DATA_DIR."""
        result = _miner_data_path("192.168.1.100", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "192-168-1-100.json"))

    def test_ip_returns_data_dir_prefix(self) -> None:
        """Result starts with DATA_DIR, confirming os.path.join is used."""
        result = _miner_data_path("10.0.0.1", ".json")
        self.assertTrue(result.startswith(DATA_DIR))


class TestMinerDataPathMACInput(TestCase):
    """Case B: colon-separated MAC input — colons must be replaced with dashes."""

    def test_colon_mac_log_jsonl(self) -> None:
        """_miner_data_path replaces colons with dashes for a MAC + .log.jsonl suffix."""
        result = _miner_data_path("aa:bb:cc:dd:ee:ff", ".log.jsonl")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.log.jsonl"))

    def test_colon_mac_json(self) -> None:
        """_miner_data_path replaces colons with dashes for a MAC + .json suffix."""
        result = _miner_data_path("aa:bb:cc:dd:ee:ff", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.json"))

    def test_colon_mac_uppercase_no_lowercasing(self) -> None:
        """_miner_data_path replaces colons with dashes but does NOT lowercase (permissive)."""
        result = _miner_data_path("AA:BB:CC:DD:EE:FF", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "AA-BB-CC-DD-EE-FF.json"))


class TestMinerDataPathSynthID(TestCase):
    """Case C: synth-ID input — no transformation needed (already dashes + hex)."""

    def test_synth_id_metrics_db(self) -> None:
        """_miner_data_path passes synth IDs through verbatim (no colons or dots to replace)."""
        result = _miner_data_path("syn-192-168-1-100-a1b2c3d4", ".metrics.db")
        self.assertEqual(result, os.path.join(DATA_DIR, "syn-192-168-1-100-a1b2c3d4.metrics.db"))

    def test_synth_id_json(self) -> None:
        """_miner_data_path leaves synth IDs intact with .json suffix."""
        result = _miner_data_path("syn-192-168-1-100-a1b2c3d4", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "syn-192-168-1-100-a1b2c3d4.json"))


class TestMinerDataPathAlreadyDashedMAC(TestCase):
    """Case D: already-dashed MAC input — idempotent (no colons to replace)."""

    def test_dashed_mac_json(self) -> None:
        """_miner_data_path is idempotent when the MAC is already dash-separated."""
        result = _miner_data_path("aa-bb-cc-dd-ee-ff", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.json"))


class TestMinerDataPathMultiPartSuffix(TestCase):
    """Case E: multi-part suffix (.checkpoint.json) — both dot segments preserved."""

    def test_ip_checkpoint_json(self) -> None:
        """_miner_data_path produces correct path with multi-part .checkpoint.json suffix."""
        result = _miner_data_path("192.168.1.100", ".checkpoint.json")
        self.assertEqual(result, os.path.join(DATA_DIR, "192-168-1-100.checkpoint.json"))

    def test_ip_stock_json(self) -> None:
        """_miner_data_path produces correct path with .stock.json suffix."""
        result = _miner_data_path("192.168.1.100", ".stock.json")
        self.assertEqual(result, os.path.join(DATA_DIR, "192-168-1-100.stock.json"))


class TestMinerDataPathEmptySuffix(TestCase):
    """Case F: empty suffix — no trailing character appended after identifier."""

    def test_mac_empty_suffix(self) -> None:
        """_miner_data_path with empty suffix returns dashed MAC alone with no trailing char."""
        result = _miner_data_path("aa:bb:cc:dd:ee:ff", "")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff"))

    def test_ip_empty_suffix(self) -> None:
        """_miner_data_path with IP + empty suffix returns dashed IP alone with no trailing char."""
        result = _miner_data_path("192.168.1.1", "")
        self.assertEqual(result, os.path.join(DATA_DIR, "192-168-1-1"))


class TestMinerDataPathPermissive(TestCase):
    """Case G: permissive transform — arbitrary identifier passes through (beyond . / : → -)."""

    def test_arbitrary_identifier_no_dots_no_colons(self) -> None:
        """_miner_data_path does not validate beyond replacing dots and colons."""
        result = _miner_data_path("anything-goes-here", ".test")
        self.assertEqual(result, os.path.join(DATA_DIR, "anything-goes-here.test"))

    def test_identifier_with_dots_becomes_dashes(self) -> None:
        """_miner_data_path replaces every dot in the identifier with a dash."""
        result = _miner_data_path("some.dotted.ident", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "some-dotted-ident.json"))

    def test_identifier_with_colons_becomes_dashes(self) -> None:
        """_miner_data_path replaces every colon in the identifier with a dash."""
        result = _miner_data_path("some:colons:here", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "some-colons-here.json"))


class TestMinerPlatformPathHappyPath(TestCase):
    """Case H: _miner_platform_path returns MAC + firmware + suffix joined under DATA_DIR."""

    def test_mac_epic_profile_json(self) -> None:
        """_miner_platform_path produces <dashed-mac>.epic.profile.json under DATA_DIR."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "epic", ".profile.json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.epic.profile.json"))

    def test_result_starts_with_data_dir(self) -> None:
        """_miner_platform_path result starts with DATA_DIR prefix."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "epic", ".json")
        self.assertTrue(result.startswith(DATA_DIR))


class TestMinerPlatformPathFirmwareTypes(TestCase):
    """Case I: supported firmware type strings appear verbatim in filenames."""

    def test_firmware_epic(self) -> None:
        """_miner_platform_path embeds 'epic' verbatim between MAC and suffix."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "epic", ".json")
        self.assertIn(".epic.", os.path.basename(result))

    def test_firmware_bixbit(self) -> None:
        """_miner_platform_path embeds 'bixbit' verbatim between MAC and suffix."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "bixbit", ".json")
        self.assertIn(".bixbit.", os.path.basename(result))

    def test_firmware_luxos(self) -> None:
        """_miner_platform_path embeds 'luxos' verbatim between MAC and suffix."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "luxos", ".json")
        self.assertIn(".luxos.", os.path.basename(result))

    def test_firmware_braiins(self) -> None:
        """_miner_platform_path embeds 'braiins' verbatim between MAC and suffix."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "braiins", ".json")
        self.assertIn(".braiins.", os.path.basename(result))

    def test_firmware_whatsminer(self) -> None:
        """_miner_platform_path embeds 'whatsminer' between MAC and suffix."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "whatsminer", ".json")
        self.assertIn(".whatsminer.", os.path.basename(result))

    def test_firmware_appears_before_suffix(self) -> None:
        """_miner_platform_path places firmware before the suffix in the filename."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "epic", ".checkpoint.json")
        self.assertTrue(os.path.basename(result).endswith(".epic.checkpoint.json"))


class TestMinerPlatformPathMACNormalization(TestCase):
    """Case J: MAC normalization via _mac_for_filename — uppercase lowercased, colons → dashes."""

    def test_uppercase_mac_lowercased(self) -> None:
        """_miner_platform_path lowercases an uppercase colon-separated MAC."""
        result = _miner_platform_path("AA:BB:CC:DD:EE:FF", "epic", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.epic.json"))

    def test_mixed_case_mac_lowercased(self) -> None:
        """_miner_platform_path lowercases a mixed-case MAC."""
        result = _miner_platform_path("Aa:Bb:Cc:Dd:Ee:Ff", "epic", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.epic.json"))

    def test_dashed_mac_accepted(self) -> None:
        """_miner_platform_path accepts a dash-separated MAC (normalizes via _mac_for_filename)."""
        result = _miner_platform_path("aa-bb-cc-dd-ee-ff", "epic", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.epic.json"))

    def test_bare_mac_accepted(self) -> None:
        """_miner_platform_path accepts a bare 12-char hex MAC."""
        result = _miner_platform_path("aabbccddeeff", "epic", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.epic.json"))


class TestMinerPlatformPathSynthID(TestCase):
    """Case K: synth IDs pass through _mac_for_filename and appear verbatim in filename."""

    def test_synth_id_luxos_stock_json(self) -> None:
        """_miner_platform_path produces synth-form filename + .luxos.stock.json suffix."""
        result = _miner_platform_path("syn-192-168-1-100-a1b2c3d4", "luxos", ".stock.json")
        self.assertEqual(
            result,
            os.path.join(DATA_DIR, "syn-192-168-1-100-a1b2c3d4.luxos.stock.json"),
        )

    def test_synth_id_epic_checkpoint_json(self) -> None:
        """_miner_platform_path produces synth-form filename + .epic.checkpoint.json suffix."""
        result = _miner_platform_path("syn-192-168-1-100-a1b2c3d4", "epic", ".checkpoint.json")
        self.assertEqual(
            result,
            os.path.join(DATA_DIR, "syn-192-168-1-100-a1b2c3d4.epic.checkpoint.json"),
        )


class TestMinerPlatformPathMalformedMAC(TestCase):
    """Case L: malformed MAC raises ValueError (delegated through _mac_for_filename)."""

    def test_not_a_mac_raises_value_error(self) -> None:
        """_miner_platform_path raises ValueError for a non-MAC identifier."""
        with self.assertRaises(ValueError):
            _miner_platform_path("not-a-mac", "epic", ".json")

    def test_too_few_octets_raises_value_error(self) -> None:
        """_miner_platform_path raises ValueError for fewer-than-6-octet MACs."""
        with self.assertRaises(ValueError):
            _miner_platform_path("aa:bb:cc:dd", "epic", ".json")

    def test_too_many_octets_raises_value_error(self) -> None:
        """_miner_platform_path raises ValueError for more-than-6-octet MACs."""
        with self.assertRaises(ValueError):
            _miner_platform_path("aa:bb:cc:dd:ee:ff:00", "epic", ".json")

    def test_non_hex_octet_raises_value_error(self) -> None:
        """_miner_platform_path raises ValueError when an octet contains non-hex characters."""
        with self.assertRaises(ValueError):
            _miner_platform_path("aa:bb:cc:dd:ee:zz", "epic", ".json")

    def test_empty_string_raises_value_error(self) -> None:
        """_miner_platform_path raises ValueError for an empty MAC string."""
        with self.assertRaises(ValueError):
            _miner_platform_path("", "epic", ".json")


class TestMinerPlatformPathNonStringMAC(TestCase):
    """Case M: non-string MAC raises TypeError (delegated through _mac_for_filename)."""

    def test_none_mac_raises_type_error(self) -> None:
        """_miner_platform_path raises TypeError when MAC is None."""
        with self.assertRaises(TypeError):
            _miner_platform_path(None, "epic", ".json")  # type: ignore[arg-type]

    def test_int_mac_raises_type_error(self) -> None:
        """_miner_platform_path raises TypeError when MAC is an integer."""
        with self.assertRaises(TypeError):
            _miner_platform_path(42, "epic", ".json")  # type: ignore[arg-type]

    def test_bytes_mac_raises_type_error(self) -> None:
        """_miner_platform_path raises TypeError when MAC is bytes."""
        with self.assertRaises(TypeError):
            _miner_platform_path(b"aa:bb:cc:dd:ee:ff", "epic", ".json")  # type: ignore[arg-type]


class TestMinerPlatformPathFirmwareNotValidated(TestCase):
    """Case N: firmware string is concatenated verbatim — no validation, no exception."""

    def test_arbitrary_firmware_string_no_raise(self) -> None:
        """_miner_platform_path does not raise for an unrecognized firmware string."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "anyfirmware", ".json")
        self.assertEqual(result, os.path.join(DATA_DIR, "aa-bb-cc-dd-ee-ff.anyfirmware.json"))


class TestMinerPlatformPathDataDirPrefix(TestCase):
    """Case O: both helpers use os.path.join(DATA_DIR, ...) — result is in DATA_DIR."""

    def test_data_path_has_data_dir(self) -> None:
        """_miner_data_path result's directory equals DATA_DIR."""
        result = _miner_data_path("192.168.1.100", ".json")
        self.assertEqual(os.path.dirname(result), DATA_DIR)

    def test_platform_path_has_data_dir(self) -> None:
        """_miner_platform_path result's directory equals DATA_DIR."""
        result = _miner_platform_path("aa:bb:cc:dd:ee:ff", "epic", ".json")
        self.assertEqual(os.path.dirname(result), DATA_DIR)

    def test_data_dir_is_absolute(self) -> None:
        """DATA_DIR is stable even if the process later changes working directory."""
        self.assertTrue(os.path.isabs(DATA_DIR))
