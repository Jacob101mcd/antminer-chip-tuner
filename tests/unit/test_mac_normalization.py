"""Tests for _normalize_mac and _mac_for_filename in tuner_app.constants.

Spec contract:
  _normalize_mac(raw) -> str
    Canonicalize a MAC-like string to lowercase colon-separated form.
    Accepts colon, dash, or no-separator forms; uppercase or lowercase.
    Strips whitespace. Synthesized "syn-..." IDs pass through unchanged
    (whitespace-stripped). Raises ValueError on invalid input.

  _mac_for_filename(mac) -> str
    Convert a canonical colon-separated MAC to filesystem-safe dash form.
    Synthesized "syn-..." IDs (already dashed) pass through unchanged.
    Idempotent for dash-form input. Validates via _normalize_mac;
    raises TypeError/ValueError on bad input.

Round-trip: _normalize_mac(_mac_for_filename(canonical)) == canonical
for any canonical input (real MAC or synth ID).
"""

from __future__ import annotations

from unittest import TestCase

from tuner_app.constants import _mac_for_filename, _normalize_mac


class NormalizeMacColonCanonical(TestCase):
    """Tests for _normalize_mac with already-canonical colon-separated input."""

    def test_canonical_lowercase_colon_returned_unchanged(self) -> None:
        """Already-canonical lowercase colon-separated MAC is returned as-is."""
        self.assertEqual(_normalize_mac("aa:bb:cc:dd:ee:ff"), "aa:bb:cc:dd:ee:ff")


class NormalizeMacUppercaseColons(TestCase):
    """Tests for _normalize_mac with uppercase colon-separated input."""

    def test_uppercase_colon_mac_lowercased(self) -> None:
        """Uppercase colon-separated MAC is lowercased to canonical form."""
        self.assertEqual(_normalize_mac("AA:BB:CC:DD:EE:FF"), "aa:bb:cc:dd:ee:ff")


class NormalizeMacDashedInput(TestCase):
    """Tests for _normalize_mac with lowercase dash-separated input."""

    def test_lowercase_dashed_mac_converted_to_colons(self) -> None:
        """Lowercase dash-separated MAC is converted to colon-separated canonical form."""
        self.assertEqual(_normalize_mac("aa-bb-cc-dd-ee-ff"), "aa:bb:cc:dd:ee:ff")


class NormalizeMacUppercaseDashed(TestCase):
    """Tests for _normalize_mac with uppercase dash-separated input."""

    def test_uppercase_dashed_mac_lowercased_and_converted(self) -> None:
        """Uppercase dash-separated MAC is lowercased and converted to colon-separated form."""
        self.assertEqual(_normalize_mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")


class NormalizeMacBare12Hex(TestCase):
    """Tests for _normalize_mac with bare 12-hex-character input (no separators)."""

    def test_bare_lowercase_12hex_grouped_with_colons(self) -> None:
        """Bare 12-hex-char string (no separators) is grouped into colon-separated octets."""
        self.assertEqual(_normalize_mac("aabbccddeeff"), "aa:bb:cc:dd:ee:ff")


class NormalizeMacUppercaseBare12Hex(TestCase):
    """Tests for _normalize_mac with bare uppercase 12-hex-character input."""

    def test_bare_uppercase_12hex_lowercased_and_grouped(self) -> None:
        """Bare uppercase 12-hex string is lowercased and grouped into colon-separated octets."""
        self.assertEqual(_normalize_mac("AABBCCDDEEFF"), "aa:bb:cc:dd:ee:ff")


class NormalizeMacMixedCase(TestCase):
    """Tests for _normalize_mac with mixed-case colon-separated input."""

    def test_mixed_case_colon_mac_lowercased(self) -> None:
        """Mixed-case colon-separated MAC is fully lowercased to canonical form."""
        self.assertEqual(_normalize_mac("aa:bb:cc:DD:ee:FF"), "aa:bb:cc:dd:ee:ff")


class NormalizeMacWhitespaceStripped(TestCase):
    """Tests for _normalize_mac with leading and trailing whitespace."""

    def test_leading_trailing_whitespace_stripped_before_normalizing(self) -> None:
        """Leading and trailing whitespace is stripped before normalization."""
        self.assertEqual(_normalize_mac("  aa:bb:cc:dd:ee:ff  "), "aa:bb:cc:dd:ee:ff")


class NormalizeMacSynthId(TestCase):
    """Tests for _normalize_mac with synthesized syn-... identifiers."""

    def test_synth_id_returned_unchanged(self) -> None:
        """A synthesized syn-... identifier is returned unchanged."""
        self.assertEqual(
            _normalize_mac("syn-192-168-6-122-a1b2c3d4"),
            "syn-192-168-6-122-a1b2c3d4",
        )


class NormalizeMacWhitespaceSynthId(TestCase):
    """Tests for _normalize_mac with whitespace-wrapped synthesized identifiers."""

    def test_whitespace_wrapped_synth_id_stripped_body_unchanged(self) -> None:
        """Whitespace around a synthesized syn-... identifier is stripped; body unchanged."""
        self.assertEqual(
            _normalize_mac("  syn-192-168-6-122-a1b2c3d4  "),
            "syn-192-168-6-122-a1b2c3d4",
        )


class NormalizeMacMalformedSynthId(TestCase):
    """Tests for _normalize_mac rejection of synth IDs containing forbidden characters."""

    def test_synth_id_with_path_traversal_raises_value_error(self) -> None:
        """A synth ID containing '../' (path traversal) raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("syn-../etc/passwd")

    def test_synth_id_with_slash_raises_value_error(self) -> None:
        """A synth ID containing forward slash (filename-unsafe) raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("syn-foo/bar")

    def test_empty_synth_id_raises_value_error(self) -> None:
        """A bare 'syn-' with no remainder raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("syn-")


class NormalizeMacEmptyString(TestCase):
    """Tests for _normalize_mac rejection of empty string input."""

    def test_empty_string_raises_value_error(self) -> None:
        """An empty string raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("")


class NormalizeMacWhitespaceOnly(TestCase):
    """Tests for _normalize_mac rejection of whitespace-only string input."""

    def test_whitespace_only_string_raises_value_error(self) -> None:
        """A whitespace-only string raises ValueError after stripping yields an empty string."""
        with self.assertRaises(ValueError):
            _normalize_mac("   ")


class NormalizeMacTooFewOctets(TestCase):
    """Tests for _normalize_mac rejection of strings with fewer than 6 octets."""

    def test_five_octet_colon_mac_raises_value_error(self) -> None:
        """A colon-separated string with only 5 octets raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aa:bb:cc:dd:ee")

    def test_five_octet_dashed_mac_raises_value_error(self) -> None:
        """A dash-separated string with only 5 octets raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aa-bb-cc-dd-ee")


class NormalizeMacTooManyOctets(TestCase):
    """Tests for _normalize_mac rejection of strings with more than 6 colon-octets."""

    def test_seven_octet_colon_mac_raises_value_error(self) -> None:
        """A colon-separated string with 7 octets raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aa:bb:cc:dd:ee:ff:00")


class NormalizeMacNonHexColonForm(TestCase):
    """Tests for _normalize_mac rejection of non-hex characters in colon-separated form."""

    def test_non_hex_char_in_colon_form_raises_value_error(self) -> None:
        """A colon-separated MAC containing a non-hex character raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("gg:bb:cc:dd:ee:ff")


class NormalizeMacNonHexBareForm(TestCase):
    """Tests for _normalize_mac rejection of non-hex characters in bare (no separator) form."""

    def test_non_hex_char_in_bare_form_raises_value_error(self) -> None:
        """A bare 12-char string containing a non-hex character raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aabbccddeefg")


class NormalizeMacOddLengthBareForm(TestCase):
    """Tests for _normalize_mac rejection of odd-length bare strings."""

    def test_11_char_bare_string_raises_value_error(self) -> None:
        """An 11-character bare string (odd length) raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aabbccddeef")


class NormalizeMacWrongLengthBareForm(TestCase):
    """Tests for _normalize_mac rejection of bare strings with wrong length."""

    def test_14_char_bare_string_raises_value_error(self) -> None:
        """A 14-character bare string raises ValueError because it cannot map to 6 octets."""
        with self.assertRaises(ValueError):
            _normalize_mac("aabbccddeefff0")

    def test_10_char_bare_string_raises_value_error(self) -> None:
        """A 10-character bare string (5 octets) raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aabbccddee")

    def test_2_char_bare_string_raises_value_error(self) -> None:
        """A 2-character bare string (1 octet) raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aa")


class NormalizeMacMixedSeparators(TestCase):
    """Tests for _normalize_mac rejection of strings mixing colon and dash separators."""

    def test_mixed_colon_and_dash_separators_raise_value_error(self) -> None:
        """A MAC string mixing colon and dash separators raises ValueError."""
        with self.assertRaises(ValueError):
            _normalize_mac("aa:bb-cc:dd-ee:ff")


class NormalizeMacNonStringInput(TestCase):
    """Tests for _normalize_mac rejection of non-string input."""

    def test_integer_input_raises_value_error_or_type_error(self) -> None:
        """A non-string input such as an integer raises ValueError or TypeError."""
        with self.assertRaises((ValueError, TypeError)):
            _normalize_mac(123)  # type: ignore[arg-type]

    def test_none_input_raises_value_error_or_type_error(self) -> None:
        """A None input raises ValueError or TypeError."""
        with self.assertRaises((ValueError, TypeError)):
            _normalize_mac(None)  # type: ignore[arg-type]

    def test_bytes_input_raises_value_error_or_type_error(self) -> None:
        """A bytes input raises ValueError or TypeError (str is required)."""
        with self.assertRaises((ValueError, TypeError)):
            _normalize_mac(b"aa:bb:cc:dd:ee:ff")  # type: ignore[arg-type]


class MacForFilenameColonMac(TestCase):
    """Tests for _mac_for_filename with canonical colon-separated MAC input."""

    def test_canonical_colon_mac_colons_replaced_by_dashes(self) -> None:
        """A canonical colon-separated MAC has its colons replaced by dashes."""
        self.assertEqual(_mac_for_filename("aa:bb:cc:dd:ee:ff"), "aa-bb-cc-dd-ee-ff")


class MacForFilenameSynthId(TestCase):
    """Tests for _mac_for_filename with synthesized syn-... identifier input."""

    def test_synth_id_passes_through_unchanged(self) -> None:
        """A synthesized syn-... identifier passes through _mac_for_filename unchanged."""
        self.assertEqual(
            _mac_for_filename("syn-192-168-6-122-a1b2c3d4"),
            "syn-192-168-6-122-a1b2c3d4",
        )


class MacForFilenameAlreadyDashed(TestCase):
    """Tests for _mac_for_filename idempotency on already-dashed input."""

    def test_already_dashed_mac_returned_unchanged(self) -> None:
        """An already dash-separated MAC-form string is returned unchanged (idempotent)."""
        self.assertEqual(_mac_for_filename("aa-bb-cc-dd-ee-ff"), "aa-bb-cc-dd-ee-ff")


class RoundTripNormalizeThenFilename(TestCase):
    """Round-trip: _normalize_mac(_mac_for_filename(canonical)) returns the original canonical."""

    def test_round_trip_real_mac(self) -> None:
        """_normalize_mac applied to the filename form of a real canonical MAC returns canonical."""
        canonical = "aa:bb:cc:dd:ee:ff"
        self.assertEqual(_normalize_mac(_mac_for_filename(canonical)), canonical)

    def test_round_trip_synth_id(self) -> None:
        """Filename form of a synth ID round-trips back to the original synth ID."""
        synth = "syn-192-168-6-122-a1b2c3d4"
        self.assertEqual(_normalize_mac(_mac_for_filename(synth)), synth)

    def test_round_trip_uppercase_mac_reduced_to_canonical_first(self) -> None:
        """_normalize_mac on filename form of canonical (from uppercase) MAC returns canonical."""
        canonical = _normalize_mac("AA:BB:CC:DD:EE:FF")
        self.assertEqual(_normalize_mac(_mac_for_filename(canonical)), canonical)
