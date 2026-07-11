"""
Session token issuance, validation, and revocation.

Sessions live in-memory only (`state._sessions`), keyed by token, mapping to
expiry timestamps. Validation slides the expiry forward on hit. A simple
counter-driven GC sweep purges expired entries every ~50 issuances. The
process restarts wipe all sessions by design — there's no persistence layer.
"""

from __future__ import annotations

import secrets
import time

from tuner_app import state
from tuner_app.constants import SESSION_TTL_SEC


def issue_session() -> str:
    """Create a new session token. Opportunistically GCs expired sessions."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with state._sessions_lock:
        state._sessions[token] = now + SESSION_TTL_SEC
        state._session_gc_counter += 1
        if state._session_gc_counter >= 50:
            state._session_gc_counter = 0
            expired = [t for t, exp in state._sessions.items() if exp < now]
            for t in expired:
                state._sessions.pop(t, None)
    return token


def validate_session(token: str | None) -> bool:
    """Return True if `token` refers to a live session; extend it on hit."""
    if not token:
        return False
    now = time.time()
    with state._sessions_lock:
        exp = state._sessions.get(token)
        if exp is None:
            return False
        if exp < now:
            state._sessions.pop(token, None)
            return False
        # Sliding expiration: each hit pushes expiry forward.
        state._sessions[token] = now + SESSION_TTL_SEC
        return True


def revoke_session(token: str | None) -> None:
    """Delete a session token. No-op for None / empty / unknown tokens."""
    if not token:
        return
    with state._sessions_lock:
        state._sessions.pop(token, None)
