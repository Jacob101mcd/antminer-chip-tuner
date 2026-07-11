"""
Brute-force protection for the login endpoint.

Tracks failed login attempts per client IP. After
`LOGIN_LOCKOUT_THRESHOLD` failures within `LOGIN_LOCKOUT_WINDOW_SEC`,
subsequent attempts from that IP are rejected regardless of password
correctness. The login endpoint also adds a `time.sleep(2)` delay before
returning the rejection so attackers can't churn through credentials.
"""

from __future__ import annotations

import time

from tuner_app import state
from tuner_app.constants import LOGIN_LOCKOUT_THRESHOLD, LOGIN_LOCKOUT_WINDOW_SEC


def is_login_blocked(client_ip: str | None) -> bool:
    """Return True if the client IP is currently rate-limited from logging in."""
    if not client_ip:
        return False
    now = time.time()
    with state._login_attempts_lock:
        entry = state._login_attempts.get(client_ip)
        if not entry:
            return False
        fails, first = entry
        if now - first > LOGIN_LOCKOUT_WINDOW_SEC:
            state._login_attempts.pop(client_ip, None)
            return False
        return fails >= LOGIN_LOCKOUT_THRESHOLD


def record_login_failure(client_ip: str | None) -> None:
    """Increment the failure counter for `client_ip`. Resets the counter when
    the previous failure was outside the lockout window (a fresh window starts)."""
    if not client_ip:
        return
    now = time.time()
    with state._login_attempts_lock:
        entry = state._login_attempts.get(client_ip)
        if not entry or now - entry[1] > LOGIN_LOCKOUT_WINDOW_SEC:
            state._login_attempts[client_ip] = (1, now)
        else:
            state._login_attempts[client_ip] = (entry[0] + 1, entry[1])


def record_login_success(client_ip: str | None) -> None:
    """Clear the failure counter for `client_ip` after a successful login."""
    if not client_ip:
        return
    with state._login_attempts_lock:
        state._login_attempts.pop(client_ip, None)
