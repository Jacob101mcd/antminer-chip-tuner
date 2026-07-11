"""Credential redaction regressions for public and persisted data."""

from __future__ import annotations

import json

from tuner_app.privacy import REDACTED, is_secret_key, sanitize


def test_sensitive_key_matching_is_case_and_separator_insensitive():
    for key in (
        "PASSWORD",
        "scan-passwords",
        "MRR_API_KEY",
        "mrr api secret",
        "MINERSTAT_API_KEY",
        "password_hash",
        "Set-Cookie",
        "session_token",
    ):
        assert is_secret_key(key)


def test_sanitize_drops_nested_secret_keys_and_scrubs_secret_values_in_text():
    secrets = frozenset(
        {
            "miner-password-value",
            "mrr-secret-value",
            "session-token-value",
        }
    )
    payload = {
        "PASSWORD": "miner-password-value",
        "nested": {
            "MRR_API_SECRET": "mrr-secret-value",
            "safe": "request failed for mrr-secret-value",
        },
        "list": [
            {"Set-Cookie": "session-token-value"},
            "session-token-value",
        ],
    }

    cleaned = sanitize(payload, secrets=secrets)
    serialized = json.dumps(cleaned)
    assert "PASSWORD" not in serialized
    assert "MRR_API_SECRET" not in serialized
    assert "Set-Cookie" not in serialized
    assert not any(secret in serialized for secret in secrets)
    assert cleaned["nested"]["safe"] == f"request failed for {REDACTED}"
    assert cleaned["list"] == [{}, REDACTED]
