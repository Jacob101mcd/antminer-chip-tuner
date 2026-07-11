"""MRR v2 API client. Stateless, HMAC-SHA1 signed, raises MRRError on failure."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json

from tuner_app.miner.exceptions import MinerCommandError, MRRError
from tuner_app.mrr.nonce import _mrr_nonce
from tuner_app.net.response_limits import read_capped_http_response


class MRRClient:
    """Stateless MRR v2 API client. Every method issues a single signed HTTPS
    request and returns parsed JSON (or raises MRRError). Callers supply key
    and secret at construction; a fresh client is cheap (~0 allocations beyond
    string storage) so constructing one per call is fine.

    Endpoint reference (what we actually use):
        GET  /whoami              — credential + permissions check
        GET  /rig/mine            — list owned rigs (for the rig-ID picker)
        GET  /rig/{id}            — fetch a single rig (for the rented-check)
        POST /rig/batch           — update status and/or advertised hashrate
    """

    HOST = "www.miningrigrentals.com"
    API_PREFIX = "/api/v2"

    def __init__(self, api_key: str, api_secret: str, timeout: int = 15) -> None:
        self.api_key = (api_key or "").strip()
        self.api_secret = (api_secret or "").strip()
        self.timeout = timeout

    def _request(self, method, endpoint, body=None):
        """Issue a signed request. `endpoint` is the API path WITHOUT the
        /api/v2/ prefix and WITHOUT trailing slash (MRR hashes it that way).
        Returns the parsed top-level JSON dict; raises MRRError on any
        non-success."""
        if not self.api_key or not self.api_secret:
            raise MRRError("MRR credentials not configured")
        if not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        if endpoint.endswith("/") and len(endpoint) > 1:
            endpoint = endpoint.rstrip("/")
        nonce = str(_mrr_nonce.next())
        message = (self.api_key + nonce + endpoint).encode("utf-8")
        sig = hmac.new(self.api_secret.encode("utf-8"), message, hashlib.sha1).hexdigest()
        headers = {
            "x-api-key": self.api_key,
            "x-api-nonce": nonce,
            "x-api-sign": sig,
            "Accept": "application/json",
            "User-Agent": "antminer-chip-tuner/0.1.0",
        }
        body_bytes = None
        if body is not None:
            body_bytes = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body_bytes))
        full_path = self.API_PREFIX + endpoint
        try:
            conn = http.client.HTTPSConnection(self.HOST, timeout=self.timeout)
            try:
                conn.request(method, full_path, body=body_bytes, headers=headers)
                resp = conn.getresponse()
                try:
                    raw = read_capped_http_response(resp)
                except MinerCommandError as exc:
                    raise MRRError("MRR response exceeded the network response limit") from exc
                status = resp.status
            finally:
                conn.close()
        except (TimeoutError, OSError) as e:
            raise MRRError(f"network error contacting MRR: {e}")  # noqa: B904
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = None
        if status == 401 or status == 403:
            raise MRRError(f"MRR rejected credentials (HTTP {status}) on {method} {endpoint}")
        if status < 200 or status >= 300:
            raise MRRError(f"MRR {method} {endpoint} returned HTTP {status}")
        if not isinstance(parsed, dict):
            raise MRRError(
                f"MRR {method} {endpoint} returned unexpected payload type "
                f"({type(parsed).__name__})"
            )
        if parsed.get("success") is False:
            raise MRRError(f"MRR {method} {endpoint} rejected the request")
        return parsed

    def whoami(self) -> dict:
        """GET /whoami — returns the inner `data` dict: {authed, userid,
        permissions: {withdraw, rent, rigs}, ...}. Used for the Test
        Connection button to verify creds before wiring the engine hooks."""
        resp = self._request("GET", "/whoami")
        return resp.get("data") or {}

    def list_my_rigs(self) -> list:
        """GET /rig/mine — list of owned rigs. Shape varies slightly across
        MRR responses; normalize to a list of rig dicts. Used to populate
        the rig-ID dropdown on the per-miner detail view."""
        resp = self._request("GET", "/rig/mine")
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Some endpoints nest records under `records` / `rigs`.
            for key in ("records", "rigs"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
            # Single-rig dict — wrap for uniform caller handling.
            if "id" in data:
                return [data]
        return []

    def get_rig(self, rig_id: int | str) -> dict:
        """GET /rig/{id} — fetch a single rig. Returns the rig dict (NOT the
        wrapper). Raises MRRError if the rig isn't found. Used for the
        rented-check before issuing a write — we don't want to flip status
        on a rig that's currently under an active rental."""
        rid = int(rig_id)
        resp = self._request("GET", f"/rig/{rid}")
        data = resp.get("data")
        if isinstance(data, list):
            if not data:
                raise MRRError(f"rig {rid} not found")
            return data[0]
        if isinstance(data, dict):
            return data
        raise MRRError(f"rig {rid} response had unexpected shape")

    def update_rig(
        self,
        rig_id: int | str,
        status: str | None = None,
        hashrate_value: float | None = None,
        hashrate_unit: str = "th",
    ) -> dict:
        """POST /rig/batch — update status and/or advertised hashrate in a
        single call. Either `status` (enabled|disabled) or `hashrate_value`
        (numeric in `hashrate_unit` units) may be None to skip that field.
        Returns the parsed response on success."""
        if status is None and hashrate_value is None:
            raise MRRError("update_rig called with nothing to update")
        if status is not None and status not in ("enabled", "disabled"):
            raise MRRError(f"status must be enabled or disabled (got {status!r})")
        rig_entry = {"id": int(rig_id)}
        if status is not None:
            rig_entry["status"] = status
        if hashrate_value is not None:
            rig_entry["hash"] = {
                "hash": round(float(hashrate_value), 3),
                "type": hashrate_unit,
            }
        body = {"rigs": [rig_entry]}
        # `POST /rig/batch` per the MRR v2 doc: "Update a batch of rigs using
        # a 'rigs' array". MRR has no single-rig update endpoint — all
        # updates go through batch, even for a single rig.
        return self._request("POST", "/rig/batch", body=body)
