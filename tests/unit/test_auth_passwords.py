"""Unit tests for tuner_app.auth.passwords.

Covers:
- hash_password: rejects empty/None/non-string inputs (raises ValueError)
- hash_password: salt is freshly generated per call (two hashes of same
  password differ)
- verify_password: round-trip with hash_password returns True
- verify_password: wrong password returns False
- verify_password: malformed stored strings (wrong prefix, wrong field
  count, bad base64, non-int parameters) return False instead of raising
- verify_password: relies on hmac.compare_digest for constant-time match
"""

from __future__ import annotations

import unittest
import unittest.mock

from tuner_app.auth import passwords


class TestHashPassword(unittest.TestCase):
    """Tests for the hash_password function."""

    def test_hash_then_verify_roundtrip(self) -> None:
        """Hash a password and verify it matches."""
        p = "test_password"
        hashed = passwords.hash_password(p)
        self.assertTrue(passwords.verify_password(p, hashed))

    def test_empty_password_raises(self) -> None:
        """Empty string password raises ValueError."""
        with self.assertRaises(ValueError):
            passwords.hash_password("")

    def test_none_password_raises(self) -> None:
        """None password raises ValueError."""
        with self.assertRaises(ValueError):
            passwords.hash_password(None)  # type: ignore[arg-type]

    def test_non_string_password_raises(self) -> None:
        """Non-string password raises ValueError."""
        with self.assertRaises(ValueError):
            passwords.hash_password(123)  # type: ignore[arg-type]

    def test_salt_randomness(self) -> None:
        """Hashing the same password twice produces different results."""
        p = "same_password"
        h1 = passwords.hash_password(p)
        h2 = passwords.hash_password(p)
        self.assertNotEqual(h1, h2)


class TestVerifyPassword(unittest.TestCase):
    """Tests for the verify_password function."""

    def test_wrong_password_fails_verification(self) -> None:
        """Verifying a wrong password returns False."""
        correct = "correct_password"
        hashed = passwords.hash_password(correct)
        self.assertFalse(passwords.verify_password("wrong_password", hashed))

    def test_empty_stored_returns_false(self) -> None:
        """Empty stored string returns False."""
        self.assertFalse(passwords.verify_password("p", ""))

    def test_none_stored_returns_false(self) -> None:
        """None stored string returns False."""
        self.assertFalse(passwords.verify_password("p", None))  # type: ignore[arg-type]

    def test_wrong_prefix_returns_false(self) -> None:
        """Stored string with wrong prefix returns False."""
        self.assertFalse(passwords.verify_password("p", "not_scrypt$junk"))

    def test_wrong_field_count_returns_false(self) -> None:
        """Stored string with wrong number of fields returns False."""
        self.assertFalse(passwords.verify_password("p", "scrypt$bad"))

    def test_non_int_n_returns_false(self) -> None:
        """Stored string with non-int N returns False."""
        self.assertFalse(passwords.verify_password("p", "scrypt$abc$8$1$xx$yy"))

    def test_invalid_base64_salt_returns_false(self) -> None:
        """Stored string with invalid base64 salt returns False."""
        self.assertFalse(passwords.verify_password("p", "scrypt$16384$8$1$!!!$yy"))


class TestHmacCompareDigestUsed(unittest.TestCase):
    """Tests that hmac.compare_digest is used during verification."""

    def test_hmac_compare_digest_called(self) -> None:
        """verify_password calls hmac.compare_digest."""
        p = "test"
        hashed = passwords.hash_password(p)
        with unittest.mock.patch.object(
            passwords.hmac, "compare_digest", wraps=passwords.hmac.compare_digest
        ) as m:
            passwords.verify_password(p, hashed)
            m.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
