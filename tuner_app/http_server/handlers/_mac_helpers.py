"""MAC parsing + validation helpers shared by all v4 route handlers.

Every route that takes a per-miner identifier (path segment or POST body
field) goes through these so the canonical MAC lookup behavior is
consistent: malformed input → HTTP 400, valid input → colon-form MAC
that ``state.MINER_CONFIGS`` and the manager are keyed by.
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

from tuner_app.constants import MAC_PATH_RE, _normalize_mac

logger = logging.getLogger(__name__)


def parse_mac_path_segment(handler, raw: str, prefix: str) -> str | None:
    """Validate a MAC URL segment and return the canonical colon form.

    *raw* is the path segment after stripping *prefix* (e.g. for
    ``/tuner/live/aa-bb-cc-dd-ee-ff`` pass ``raw="aa-bb-cc-dd-ee-ff"``).
    The segment is percent-decoded before validation so colon-form MACs
    sent as ``aa%3Abb%3A...`` work alongside the dashed form.
    Returns the colon-form MAC on success, or None when an HTTP error has
    already been written to *handler* (caller must just ``return``).
    """
    # Strip query string if any leaked through (status_routes.log strips it
    # via urlsplit before calling, but defense-in-depth here is cheap).
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    raw = unquote(raw).strip()
    if not raw:
        handler._json_response({"ok": False, "error": "MAC required in URL path"}, status=400)
        return None
    if not MAC_PATH_RE.match(raw):
        handler._json_response(
            {"ok": False, "error": f"invalid MAC in URL path: {raw!r}"}, status=400
        )
        return None
    try:
        return _normalize_mac(raw)
    except (ValueError, TypeError) as ex:
        handler._json_response({"ok": False, "error": f"invalid MAC in URL path: {ex}"}, status=400)
        return None


def parse_mac_body_field(handler, data: dict, response_key: str = "ok") -> str | None:
    """Extract+normalize a ``mac`` body field. Reject ``ip`` body fields.

    *data* is the parsed JSON body (must already be a dict — caller checks).
    *response_key* is the boolean field name in the error response (e.g.
    ``"started"`` for /tuner/start so the wire shape stays
    ``{started: false, error: ...}`` for backward-test-shape compat).

    Returns the canonical colon-form MAC on success, or None when an HTTP
    error has already been written. The wire shape on error is always
    ``{<response_key>: False, error: "..."}`` with HTTP 400.
    """
    # Hard cutover: the legacy {ip:...} body shape is rejected so a stale
    # frontend or curl-style script can't silently route to an unintended
    # miner via the IP-MAC reverse-lookup adapter.
    if "ip" in data:
        handler._json_response(
            {
                response_key: False,
                "error": "'ip' body field is no longer accepted; use 'mac' instead",
            },
            status=400,
        )
        return None
    raw = (data.get("mac") or "").strip()
    if not raw:
        handler._json_response({response_key: False, "error": "MAC required"}, status=400)
        return None
    try:
        return _normalize_mac(raw)
    except (ValueError, TypeError) as ex:
        handler._json_response({response_key: False, "error": f"invalid MAC: {ex}"}, status=400)
        return None


def parse_macs_body_field(handler, data: dict) -> list[str] | None:
    """Extract+normalize a ``macs`` body array. Reject ``ips`` body arrays.

    Bulk-endpoint companion to :func:`parse_mac_body_field`. Returns a list
    of canonical colon-form MACs, or None when an HTTP error has already
    been written. Empty / missing array is allowed and returns ``[]`` so
    callers handle the empty-batch path uniformly.
    """
    if "ips" in data:
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": ["'ips' body field is no longer accepted; use 'macs' instead"],
            },
            status=400,
        )
        return None
    raw_list = data.get("macs") or []
    if not isinstance(raw_list, list):
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": ["'macs' must be an array"],
            },
            status=400,
        )
        return None
    logger.info(f"parse_macs_body_field: received {len(raw_list)} raw macs")
    out: list[str] = []
    bad: list[str] = []
    for raw in raw_list:
        if not isinstance(raw, str) or not raw.strip():
            logger.info(f"parse_macs_body_field: dropping {raw!r} (empty-or-whitespace)")
            continue
        try:
            out.append(_normalize_mac(raw))
        except (ValueError, TypeError) as ex:
            logger.info(f"parse_macs_body_field: dropping {raw!r} (normalize-failed: {ex})")
            bad.append(f"{raw!r}: {ex}")
    if not out and raw_list and not bad:
        logger.warning(
            "parse_macs_body_field: ALL %d macs filtered out — bulk action will report 0/0",
            len(raw_list),
        )
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": [
                    f"all {len(raw_list)} MAC(s) in request were empty/whitespace — "
                    "selection may be stale, please reload the dashboard"
                ],
            },
            status=400,
        )
        return None
    if bad:
        handler._json_response(
            {
                "results": {},
                "summary": {"total": 0, "succeeded": 0, "failed": 0},
                "errors": [f"invalid MAC(s) in 'macs': {bad}"],
            },
            status=400,
        )
        return None
    return out
