"""
Password hashing helpers using stdlib scrypt.

Single-password auth: each install configures one password whose scrypt hash
is stored in config.json under the `auth` key. The encoded format
`scrypt$N$r$p$salt_b64$hash_b64` carries all parameters with the hash so
upgrades that tune scrypt's cost factor remain backward-compatible.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

# scrypt cost factors. Tuned for a LAN UI login (<100ms) on commodity CPUs.
_SCRYPT_N = 2**14  # CPU/memory cost; tuned for a LAN UI login (<100ms)
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    """Hash a password with scrypt. Returns 'scrypt$N$r$p$salt_b64$hash_b64'."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify of a password against a stored scrypt hash."""
    if not stored or not isinstance(stored, str) or not stored.startswith("scrypt$"):
        return False
    try:
        _, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False
