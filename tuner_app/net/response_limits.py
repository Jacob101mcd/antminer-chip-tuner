"""Shared hard limits for untrusted miner network responses."""

from __future__ import annotations

import http.client

from tuner_app.miner.exceptions import MinerCommandError

MINER_RESPONSE_HARD_CAP_BYTES = 1024 * 1024
MINER_RECV_CHUNK_BYTES = 4096


def append_capped_response(response: bytes, chunk: bytes, *, command: str) -> bytes:
    """Append a socket chunk without ever growing beyond the miner response cap."""
    if len(response) + len(chunk) > MINER_RESPONSE_HARD_CAP_BYTES:
        raise MinerCommandError(
            f"{command}: response exceeded {MINER_RESPONSE_HARD_CAP_BYTES} byte cap"
        )
    return response + chunk


def read_capped_http_response(response: http.client.HTTPResponse) -> bytes:
    """Read at most the hard cap plus one sentinel byte from an HTTP response."""
    body = response.read(MINER_RESPONSE_HARD_CAP_BYTES + 1)
    if len(body) > MINER_RESPONSE_HARD_CAP_BYTES:
        raise MinerCommandError(
            f"miner HTTP response exceeded {MINER_RESPONSE_HARD_CAP_BYTES} byte cap"
        )
    return body
