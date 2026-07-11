"""Monotonic nonce source for MRR HMAC signing, persisted to JSON file."""

from __future__ import annotations

import json
import os
import threading
import time

from tuner_app.constants import MRR_NONCE_FILE


def _atomic_json_write(path: str, payload: object, indent: int | None = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=indent)
    os.replace(tmp, path)


class MRRNonce:
    """Monotonic nonce source for MRR HMAC signing. Each call returns a value
    strictly greater than all prior values across process restarts — the value
    is persisted to disk after every increment. A clock-drift-backward event
    (NTP correction) is handled by floor-ing at (last+1), so monotonicity
    survives even if time.time() regresses."""

    def __init__(self, path: str = MRR_NONCE_FILE) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._value = self._load()

    def _load(self) -> int:
        try:
            with open(self._path) as f:
                return int(json.load(f).get("nonce", 0))
        except Exception:
            return 0

    def next(self) -> int:
        with self._lock:
            now_us = int(time.time() * 1_000_000)
            self._value = max(self._value + 1, now_us)
            try:  # noqa: SIM105
                _atomic_json_write(self._path, {"nonce": self._value}, indent=None)
            except Exception:
                # Best-effort persistence. If the disk write fails, next call's
                # max() with the new now_us still yields a strictly larger
                # nonce; we only lose the restart-survival guarantee for this
                # specific call. Better to let the API call proceed than to
                # fail because of a transient disk hiccup.
                pass
            return self._value


# Shared nonce counter — safe for concurrent use from the HTTP thread + any
# engine thread that calls MRR during a sync.
_mrr_nonce = MRRNonce()
