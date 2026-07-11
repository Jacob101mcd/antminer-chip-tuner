"""Privacy boundaries for API responses, logs, and tuning artifacts.

The operator configuration necessarily contains credentials used to talk to
miners and optional third-party services.  Those values belong only in the
0600-protected config file and process memory; they must never be copied into
status responses, tuning profiles/checkpoints, exports, or logs.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Keep this list local and deliberately explicit.  Normalisation below makes
# keys case-insensitive and treats dashes/spaces like underscores.
SECRET_KEYS = frozenset(
    {
        "password",
        "scan_passwords",
        "mrr_api_key",
        "mrr_api_secret",
        "minerstat_api_key",
        "password_hash",
        "api_key",
        "api_secret",
        "authorization",
        "proxy_authorization",
        "token",
        "access_token",
        "refresh_token",
        "session",
        "sessions",
        "session_token",
        "cookie",
        "cookies",
        "set_cookie",
        "set_cookie2",
        "auth",
    }
)


def _normalise_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def is_secret_key(key: object) -> bool:
    """Return whether *key* denotes credential/session material."""
    return _normalise_key(key) in SECRET_KEYS


def _collect_secret_scalars(value: Any, result: set[str]) -> None:
    """Collect configured secret strings without importing state at module load."""
    if isinstance(value, Mapping):
        for key, child in list(value.items()):
            if is_secret_key(key):
                if isinstance(child, str) and child:
                    result.add(child)
                elif isinstance(child, (list, tuple, set, frozenset)):
                    result.update(str(item) for item in child if isinstance(item, str) and item)
            else:
                _collect_secret_scalars(child, result)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in list(value):
            _collect_secret_scalars(child, result)


def runtime_secret_values() -> frozenset[str]:
    """Best-effort snapshot of secrets currently held by the process.

    This intentionally does not acquire ``state.config_lock``: response and
    persistence callers often already hold that non-reentrant lock.  A shallow
    race can only cause an extra value to be absent from this defence-in-depth
    text scrub; key-based removal remains authoritative.
    """
    result: set[str] = set()
    try:
        from tuner_app import state

        _collect_secret_scalars(state.CONFIG, result)
        _collect_secret_scalars(state.MINER_CONFIGS, result)
        _collect_secret_scalars(state.AUTH, result)
        # Session tokens are dict keys rather than values.
        result.update(str(token) for token in list(state._sessions) if token)
    except (AttributeError, RuntimeError, TypeError):
        # Privacy filtering is used in error paths too.  A concurrently-mutated
        # mapping must not turn a response/log failure into an application crash.
        pass
    return frozenset(result)


def redact_text(value: object, secrets: frozenset[str] | None = None) -> str:
    """Remove configured credential values from arbitrary human-readable text."""
    text = str(value)
    known = runtime_secret_values() if secrets is None else secrets
    if text in known:
        return REDACTED
    # Avoid corrupting ordinary prose for pathologically short miner passwords;
    # exact scalar matches above are still protected.
    for secret in sorted((item for item in known if len(item) >= 4), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return text


def sanitize(value: Any, *, secrets: frozenset[str] | None = None) -> Any:
    """Return a JSON-friendly deep copy with secret keys and values removed.

    Secret-bearing mapping entries are omitted instead of masked so callers do
    not accidentally treat a placeholder as a usable credential.
    """
    known = runtime_secret_values() if secrets is None else secrets
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {
            key: sanitize(child, secrets=known)
            for key, child in list(value.items())
            if not is_secret_key(key)
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize(child, secrets=known) for child in list(value)]
    if isinstance(value, str):
        return redact_text(value, known)
    return value


__all__ = [
    "REDACTED",
    "SECRET_KEYS",
    "is_secret_key",
    "redact_text",
    "runtime_secret_values",
    "sanitize",
]
