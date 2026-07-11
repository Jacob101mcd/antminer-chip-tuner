"""
HTTP client helpers for talking to miner ePIC APIs.

Wraps stdlib `http.client.HTTPConnection` with source-IP-aware binding so
multi-NIC hosts route outbound calls to the right interface (see
`tuner_app.net.source_ip` for the resolution logic).
"""

from __future__ import annotations

import http.client

from tuner_app.net.response_limits import read_capped_http_response
from tuner_app.net.source_ip import resolve_source_ip


def miner_http_request(
    ip: str,
    port: int,
    path: str,
    data: bytes | None = None,
    method: str = "GET",
    timeout: int = 15,
    *,
    source_ip: str | None = None,
) -> tuple[int, list, bytes]:
    """Send HTTP request to miner, binding to the correct local interface.

    Returns (status, headers_list, body_bytes). Raises on connection error.
    `data` should be bytes (typically JSON-encoded); Content-Type is set to
    application/json when data is provided.

    If `source_ip` is None, resolves the source IP using `resolve_source_ip(ip, port)`.
    If `source_ip` is an empty string, uses OS default routing (source_address=None).
    If `source_ip` is a non-empty string, uses it as the source address."""
    if source_ip is None:
        source_ip = resolve_source_ip(ip, port)
    source_address = (source_ip, 0) if source_ip else None
    conn = http.client.HTTPConnection(ip, port, timeout=timeout, source_address=source_address)
    try:
        headers = {"Content-Type": "application/json"} if data is not None else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_headers = resp.getheaders()
        body = read_capped_http_response(resp)
        return status, resp_headers, body
    finally:
        try:  # noqa: SIM105
            conn.close()
        except Exception:
            pass
