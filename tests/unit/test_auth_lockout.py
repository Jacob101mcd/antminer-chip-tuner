"""Unit tests for tuner_app.auth.lockout.

Covers:
- None / empty client IP is never blocked and never records
- Below threshold (< LOGIN_LOCKOUT_THRESHOLD failures) is not blocked
- At/above threshold is blocked
- Window expiry: a stale failure entry past LOGIN_LOCKOUT_WINDOW_SEC is
  treated as no entry (entry pruned, returns False)
- record_login_success clears the failure counter for that IP
- Different client IPs track independently
- record_login_failure starts a fresh count when prior failure is outside
  the lockout window
"""

from __future__ import annotations

import time
import unittest

from tuner_app import state
from tuner_app.auth import lockout


class TestAuthLockout(unittest.TestCase):
    def setUp(self):
        state._login_attempts.clear()

    def test_none_or_empty_ip_never_blocked(self):
        """None or empty IP is never blocked."""
        self.assertFalse(lockout.is_login_blocked(None))
        self.assertFalse(lockout.is_login_blocked(""))

    def test_none_or_empty_ip_never_records(self):
        """Calling record_login_failure with None or empty IP does NOT add anything."""
        lockout.record_login_failure(None)
        lockout.record_login_failure("")
        self.assertEqual(len(state._login_attempts), 0)

    def test_below_threshold_not_blocked(self):
        """Record 4 failures for IP 1.2.3.4, then is_login_blocked returns False."""
        for _ in range(4):
            lockout.record_login_failure("1.2.3.4")
        self.assertFalse(lockout.is_login_blocked("1.2.3.4"))

    def test_at_threshold_blocked(self):
        """Record 5 failures for 1.2.3.4, then is_login_blocked returns True."""
        for _ in range(5):
            lockout.record_login_failure("1.2.3.4")
        self.assertTrue(lockout.is_login_blocked("1.2.3.4"))

    def test_above_threshold_blocked(self):
        """Record 6 failures, blocked."""
        for _ in range(6):
            lockout.record_login_failure("1.2.3.4")
        self.assertTrue(lockout.is_login_blocked("1.2.3.4"))

    def test_window_expiry_resets(self):
        """Manually inject a stale entry; is_login_blocked should clear it and return False."""
        state._login_attempts["1.2.3.4"] = (10, time.time() - 999)
        result = lockout.is_login_blocked("1.2.3.4")
        self.assertFalse(result)
        self.assertNotIn("1.2.3.4", state._login_attempts)

    def test_successful_login_clears_count(self):
        """record_login_success removes the IP from state._login_attempts."""
        for _ in range(4):
            lockout.record_login_failure("1.2.3.4")
        lockout.record_login_success("1.2.3.4")
        self.assertNotIn("1.2.3.4", state._login_attempts)

    def test_different_ips_are_independent(self):
        """5 fails on IP A blocks A but does not block IP B (with no fails)."""
        for _ in range(5):
            lockout.record_login_failure("1.2.3.4")
        # IP B never fails — it should not be blocked.
        self.assertTrue(lockout.is_login_blocked("1.2.3.4"))
        self.assertFalse(lockout.is_login_blocked("5.6.7.8"))

    def test_stale_window_starts_fresh_count(self):
        """A failure record on a stale (out-of-window) entry resets to (1, now)."""
        state._login_attempts["1.2.3.4"] = (5, time.time() - 999)
        lockout.record_login_failure("1.2.3.4")
        entry = state._login_attempts["1.2.3.4"]
        self.assertEqual(entry[0], 1)
        self.assertAlmostEqual(entry[1], time.time(), delta=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
