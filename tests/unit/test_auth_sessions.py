"""Unit tests for tuner_app.auth.sessions.

Covers:
- issue_session -> validate_session roundtrip
- revoke_session deletes the token (subsequent validate is False)
- validate_session(None | '') returns False
- validate_session('unknown') returns False
- Sliding expiry: validate pushes the expiry forward by SESSION_TTL_SEC
- Expired sessions are purged on validate (entry removed from state._sessions)
- Opportunistic GC sweeps after 50 issuances (resets _session_gc_counter)
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from tuner_app import state
from tuner_app.auth import sessions


class TestAuthSessions(unittest.TestCase):
    def setUp(self):
        state._sessions.clear()
        state._session_gc_counter = 0

    def test_issue_validate_roundtrip(self):
        """Test that a session issued can be validated."""
        token = sessions.issue_session()
        self.assertTrue(sessions.validate_session(token))

    def test_revoke_kills_session(self):
        """Test that revoking a session makes it invalid."""
        token = sessions.issue_session()
        sessions.revoke_session(token)
        self.assertFalse(sessions.validate_session(token))

    def test_none_empty_token_returns_false_on_validate(self):
        """Test that None and empty string tokens return False on validate."""
        self.assertFalse(sessions.validate_session(None))
        self.assertFalse(sessions.validate_session(""))

    def test_unknown_token_returns_false_on_validate(self):
        """Test that unknown tokens return False on validate."""
        self.assertFalse(sessions.validate_session("not_a_real_token"))

    def test_sliding_expiry(self):
        """Test that validate extends the session expiry."""
        token = sessions.issue_session()
        original_expiry = state._sessions[token]
        now = time.time()
        with patch("tuner_app.auth.sessions.time.time") as mock_time:
            mock_time.return_value = now + sessions.SESSION_TTL_SEC - 1
            result = sessions.validate_session(token)
        extended_expiry = state._sessions[token]
        self.assertTrue(result)
        self.assertGreater(extended_expiry, original_expiry)
        self.assertAlmostEqual(
            extended_expiry - original_expiry, sessions.SESSION_TTL_SEC - 1, delta=1
        )

    def test_expired_session_purged_on_validate(self):
        """Test that expired sessions are removed during validation."""
        token = "test_token"
        state._sessions[token] = time.time() - 100
        self.assertIn(token, state._sessions)
        result = sessions.validate_session(token)
        self.assertFalse(result)
        self.assertNotIn(token, state._sessions)

    def test_gc_sweeps_after_50_issuances(self):
        """Test that garbage collection runs after 50 session issues."""
        # Inject expired tokens
        for i in range(5):
            state._sessions[f"expired_{i}"] = time.time() - 100
        # Issue 50 sessions; on the 50th issue the GC loop fires and the
        # counter resets to 0.
        for _ in range(50):
            sessions.issue_session()
        # Check that expired tokens are gone and counter reset
        self.assertEqual(state._session_gc_counter, 0)
        for i in range(5):
            self.assertNotIn(f"expired_{i}", state._sessions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
