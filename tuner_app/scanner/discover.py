"""Probe individual miner IPs for ePIC, Whatsminer, Bixbit, LuxOS, or Braiins firmware."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass

from tuner_app.miner.types import MinerSummary
from tuner_app.miner.whatsminer import _compute_aeskey, _encrypt
from tuner_app.net.http_client import miner_http_request
from tuner_app.net.mac_resolve import resolve_mac, synthesize_mac_id
from tuner_app.net.response_limits import MINER_RECV_CHUNK_BYTES, append_capped_response

_MAX_LUXOS_CONFIG_BYTES = 65536  # cap for _fetch_luxos_config recv buffer


@dataclass
class ProbeResult:
    """Result of probing a single IP address for a miner."""

    ip: str
    reachable: bool
    vendor_match: bool
    password_found: str | None
    hostname: str | None
    error: str | None
    firmware_type: str | None
    summary_raw: dict | None = None
    mac: str | None = None
    id_synthesized: bool = False


def _resolve_mac_or_synth(
    ip: str,
    source_ip: str,
    vendor_mac: str | None = None,
) -> tuple[str, bool]:
    """Resolve MAC via vendor API → ARP → synth fallback chain.

    Returns ``(mac_string, id_synthesized_bool)``. ``id_synthesized`` is False
    when the MAC came from a real source (vendor API or ARP), True only when
    a synthetic placeholder was generated.

    Resolution order:
      1. If ``vendor_mac`` is a non-empty truthy string, return it verbatim
         with ``id_synthesized=False``. ARP and synth are never invoked.
         Callers must pre-validate format (see ``_extract_canonical_mac`` in
         ``tuner_app/miner/types.py``); this function does NOT validate.
      2. Otherwise (None or empty string), call ``resolve_mac(ip, source_ip=...)``.
         If a real MAC is returned, use it with ``id_synthesized=False``.
      3. If ARP also fails, call ``synthesize_mac_id(ip)`` and return the
         synthetic ID with ``id_synthesized=True``.
    """
    if vendor_mac:
        return vendor_mac, False
    resolved = resolve_mac(ip, source_ip=source_ip)
    if resolved is not None:
        return resolved, False
    return synthesize_mac_id(ip), True


def _probe_whatsminer_tcp(
    ip: str, api_port: int, timeout: float, source_ip: str = ""
) -> dict | None:
    """Probe Whatsminer miner via TCP socket.

    Whatsminer fingerprint: STATUS=="S" AND Code==133 AND isinstance(Msg, dict)
    AND "salt" in Msg AND "newsalt" in Msg AND "time" in Msg
    """
    try:
        src_addr = (source_ip, 0) if source_ip else None
        with socket.create_connection(
            (ip, api_port), timeout=timeout, source_address=src_addr
        ) as sock:
            sock.settimeout(timeout)
            request = json.dumps({"cmd": "get_token"}).encode("utf-8") + b"\n"
            sock.sendall(request)
            response = b""
            while True:
                try:
                    chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                    if not chunk:
                        break
                    response = append_capped_response(response, chunk, command="Whatsminer probe")
                    try:
                        response_dict = json.loads(response.decode("utf-8"))
                        if isinstance(response_dict, dict):
                            status = response_dict.get("STATUS")
                            code = response_dict.get("Code")
                            msg = response_dict.get("Msg")
                            if (
                                status == "S"
                                and code == 133
                                and isinstance(msg, dict)
                                and "salt" in msg
                                and "newsalt" in msg
                                and "time" in msg
                            ):
                                return response_dict
                    except json.JSONDecodeError:
                        continue
                except TimeoutError:
                    break
    except Exception:
        pass
    return None


def _validate_whatsminer_password(
    ip: str,
    api_port: int,
    password: str,
    salt: str,
    timeout: float,
    source_ip: str = "",
) -> bool:
    """Validate a candidate password against a fingerprinted Whatsminer.

    Derives AES key from (password, salt), AES-encrypts {"cmd":"status"},
    sends it as {"enc":1,"data":<b64>} over a fresh TCP socket. Treats the
    response as password-VALID unless the firmware returned the diagnostic
    {"STATUS":"E","Msg":"enc json load err"} that indicates AES-key mismatch.

    NEVER raises. Returns False on any error path (probe-helper invariant).
    Does NOT log the password.
    """
    try:
        src_addr = (source_ip, 0) if source_ip else None
        with socket.create_connection(
            (ip, api_port), timeout=timeout, source_address=src_addr
        ) as sock:
            sock.settimeout(timeout)
            aeskey = _compute_aeskey(password, salt)
            api_str = json.dumps({"cmd": "status"})
            encrypted = _encrypt(api_str, aeskey)
            request = (json.dumps({"enc": 1, "data": encrypted}) + "\n").encode("utf-8")
            sock.sendall(request)
            response = b""
            while True:
                try:
                    chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                    if not chunk:
                        break
                    response = append_capped_response(
                        response, chunk, command="Whatsminer password probe"
                    )
                    try:
                        parsed = json.loads(response.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(parsed, dict):
                        return not (
                            parsed.get("STATUS") == "E" and parsed.get("Msg") == "enc json load err"
                        )
                    return False
                except TimeoutError:
                    return False
        return False
    except Exception:
        return False


def _probe_bixbit_tcp(ip: str, api_port: int, timeout: float, source_ip: str = "") -> dict | None:
    """Probe Bixbit miner via TCP socket.

    Bixbit fingerprint: top-level dict with STATUS field (value 'S' or 'E')
    """
    try:
        src_addr = (source_ip, 0) if source_ip else None
        with socket.create_connection(
            (ip, api_port), timeout=timeout, source_address=src_addr
        ) as sock:
            sock.settimeout(timeout)
            request = json.dumps({"cmd": "summary"}).encode("utf-8") + b"\n"
            sock.sendall(request)
            response = b""
            while True:
                try:
                    chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                    if not chunk:
                        break
                    response = append_capped_response(response, chunk, command="Bixbit probe")
                    try:
                        response_dict = json.loads(response.decode("utf-8"))
                        if isinstance(response_dict, dict) and isinstance(
                            response_dict.get("STATUS"), str
                        ):
                            return response_dict
                    except json.JSONDecodeError:
                        continue
                except TimeoutError:
                    break
    except Exception:
        pass
    return None


def _validate_bixbit_password(
    ip: str,
    api_port: int,
    password: str,
    timeout: float,
    source_ip: str = "",
) -> bool:
    """Connectivity check for a fingerprinted Bixbit miner.

    Bixbit's plaintext API does not currently validate passwords; this
    helper is a connectivity-check stub that proves the miner is reachable
    and speaking the Bixbit wire protocol. Code shape matches
    ``_validate_whatsminer_password`` and ``_validate_braiins_password``
    for consistency. If a future Bixbit firmware version adds
    password-bearing read commands, replace the body with a real password
    attempt.

    NEVER raises. Returns False on any error path (probe-helper invariant).
    Does NOT log the password.
    """
    try:
        src_addr = (source_ip, 0) if source_ip else None
        with socket.create_connection(
            (ip, api_port), timeout=timeout, source_address=src_addr
        ) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps({"cmd": "summary"}) + "\n").encode("utf-8"))
            buf = b""
            while True:
                chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                if not chunk:
                    break
                buf = append_capped_response(buf, chunk, command="Bixbit password probe")
                try:
                    response = json.loads(buf.decode("utf-8"))
                    return response.get("STATUS") != "E"
                except json.JSONDecodeError:
                    continue
    except Exception:
        return False
    return False


def _probe_luxos_tcp(ip: str, api_port: int, timeout: float, source_ip: str = "") -> dict | None:
    """Probe LuxOS miner via TCP socket.

    LuxOS fingerprint: STATUS is a list-of-dicts AND VERSION is a list with at least
    one element containing key 'LUXminer'

    # INVARIANT: read-only probe only; never call logon or any mutating command
    """
    try:
        src_addr = (source_ip, 0) if source_ip else None
        with socket.create_connection(
            (ip, api_port), timeout=timeout, source_address=src_addr
        ) as sock:
            sock.settimeout(timeout)
            request = json.dumps({"command": "version"}).encode("utf-8") + b"\n"
            sock.sendall(request)
            response = b""
            while True:
                try:
                    chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                    if not chunk:
                        break
                    response = append_capped_response(response, chunk, command="LuxOS probe")
                    try:
                        response_dict = json.loads(response.decode("utf-8"))
                        if isinstance(response_dict, dict):
                            # Check LuxOS fingerprint
                            lux_status = response_dict.get("STATUS")
                            lux_version = response_dict.get("VERSION")
                            if (
                                isinstance(lux_status, list)
                                and len(lux_status) > 0
                                and isinstance(lux_status[0], dict)
                                and isinstance(lux_version, list)
                                and len(lux_version) > 0
                                and "LUXminer" in lux_version[0]
                            ):
                                return response_dict
                    except json.JSONDecodeError:
                        continue
                except TimeoutError:
                    break
    except Exception:
        pass
    return None


def _validate_luxos_password(
    ip: str,
    api_port: int,
    password: str,
    timeout: float,
    source_ip: str = "",
) -> bool:
    """Validate a candidate password against a fingerprinted LuxOS miner.

    LuxOS `logon` accepts an optional ``parameter`` field carrying the
    password. On firmware versions where no admin password is set, logon
    succeeds for any input. The helper performs a best-effort ``logoff``
    after a successful logon so the per-IP session count doesn't leak
    across N password attempts. If the miner enforces password auth,
    wrong passwords are rejected here.

    NEVER raises. Returns False on any error path (probe-helper invariant).
    Does NOT log the password.
    """
    try:
        src_addr = (source_ip, 0) if source_ip else None
        with socket.create_connection(
            (ip, api_port), timeout=timeout, source_address=src_addr
        ) as sock:
            sock.settimeout(timeout)
            sock.sendall(
                (json.dumps({"command": "logon", "parameter": password}) + "\n").encode("utf-8")
            )
            buf = b""
            response: dict | None = None
            while True:
                chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                if not chunk:
                    break
                buf = append_capped_response(buf, chunk, command="LuxOS password probe")
                try:
                    response = json.loads(buf.decode("utf-8"))
                    break
                except json.JSONDecodeError:
                    continue
            if not isinstance(response, dict):
                return False

            session_id = None
            sessions = response.get("SESSION") or response.get("SESSIONS")
            if isinstance(sessions, list) and len(sessions) > 0:
                session_dict = sessions[0]
                if isinstance(session_dict, dict) and session_dict.get("SessionID"):
                    session_id = session_dict["SessionID"]

            if not session_id:
                return False

            # Best-effort logoff so per-IP session count doesn't leak
            # across N password attempts. Failure here MUST NOT change the
            # return value.
            try:
                with socket.create_connection(
                    (ip, api_port), timeout=timeout, source_address=src_addr
                ) as logoff_sock:
                    logoff_sock.settimeout(timeout)
                    logoff_sock.sendall(
                        (json.dumps({"command": "logoff", "parameter": session_id}) + "\n").encode(
                            "utf-8"
                        )
                    )
                    try:  # noqa: SIM105 — cleanup idiom; suppression here is the simplest read
                        logoff_sock.recv(4096)
                    except Exception:
                        pass
            except Exception:
                pass
            return True
    except Exception:
        return False


def _fetch_luxos_config(ip: str, api_port: int, timeout: float, source_ip: str = "") -> dict | None:
    """Fetch the LuxOS `config` cmd response over TCP for MAC extraction.

    Sends ``{"command":"config"}`` and returns the parsed JSON dict on
    success. Returns None on timeout, connection error, JSON parse error,
    response exceeding _MAX_LUXOS_CONFIG_BYTES, or any other exception.
    Never raises.

    A 1.0 s rate-gate sleep fires unconditionally at the top of the function.
    LUXminer 2026.4.3 trips into connection-refused state when two connections
    open at sub-millisecond spacing; the probe always calls _probe_luxos_tcp
    (``version`` cmd) immediately before this function, so the sleep prevents
    the second connection from being refused.

    Response data is capped at _MAX_LUXOS_CONFIG_BYTES (65536) bytes. If the
    accumulator exceeds this limit the function returns None immediately — a
    runaway or malicious response is treated the same as a parse error.

    Used post-fingerprint (in probe_miner's LuxOS branch) to extract
    ``CONFIG[0]["MACAddr"]`` for the canonical MAC. Field path verified
    live against LUXminer 2026.4.3.

    # INVARIANT: read-only probe only; never call logon or any mutating command
    """
    # LuxOS LUXminer 2026.4.3 rate-gate: connections at <1s spacing get
    # refused. Probe sends version then config in sequence; this sleep
    # prevents the second connection from being refused.
    time.sleep(1.0)
    try:
        src_addr = (source_ip, 0) if source_ip else None
        with socket.create_connection(
            (ip, api_port), timeout=timeout, source_address=src_addr
        ) as sock:
            sock.settimeout(timeout)
            request = json.dumps({"command": "config"}).encode("utf-8") + b"\n"
            sock.sendall(request)
            response = b""
            while True:
                try:
                    chunk = sock.recv(MINER_RECV_CHUNK_BYTES)
                    if not chunk:
                        break
                    if len(response) + len(chunk) > _MAX_LUXOS_CONFIG_BYTES:
                        return None  # malicious / runaway response — bail
                    response += chunk
                    try:
                        response_dict = json.loads(response.decode("utf-8"))
                        if isinstance(response_dict, dict):
                            return response_dict
                    except json.JSONDecodeError:
                        continue
                except TimeoutError:
                    break
    except Exception:
        pass
    return None


def _fetch_epic_network(ip: str, api_port: int, timeout: float, source_ip: str = "") -> dict | None:
    """Fetch GET /network response over HTTP for canonical MAC extraction.

    Returns the parsed JSON dict on HTTP 200, or None on non-200 status,
    JSON parse error, connection error, or any other exception. Never raises.

    Used post-fingerprint (in probe_miner's ePIC branch) to extract
    ``dhcp.mac_address`` for the canonical MAC. PowerPlay-BMS firmware
    exposes MAC at /network only — /summary never includes MAC.
    """
    try:
        status, _headers, body = miner_http_request(
            ip, api_port, "/network", method="GET", timeout=timeout, source_ip=source_ip
        )
        if status != 200:
            return None
        try:
            response_dict = json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            return None
        if isinstance(response_dict, dict):
            return response_dict
    except Exception:
        pass
    return None


def _probe_braiins_http(ip: str, api_port: int, timeout: float, source_ip: str = "") -> dict | None:
    """Probe Braiins OS miner via HTTP GET /api/v1/version/.

    Braiins fingerprint: top-level dict with integer `major` field.

    OpenAPI 1.3.0 — GET /api/v1/version/ (operationId: getApiVersion):
      No authentication required.
      Response 200: components.schemas.ApiVersion
        {"major": int (int64, >=0), "minor": int (int64, >=0), "patch": int (int64, >=0)}
        All three fields are required.

    Port tension note: Braiins OS typically listens on port 80; this probe uses the
    fleet api_port (default 4028 for ePIC/Bixbit). If the operator configures
    API_PORT=80 fleet-wide, Braiins miners are discoverable but ePIC/Bixbit miners
    on 4028 won't be found. Per-miner API_PORT override is the intended resolution
    path. (option-c per Run 3 plan — document the limitation, no separate probe port.)
    """
    try:
        status, _headers, body = miner_http_request(
            ip,
            api_port,
            "/api/v1/version/",
            method="GET",
            timeout=timeout,
            source_ip=source_ip,
        )
        if status != 200:
            return None
        try:
            response_dict = json.loads(body.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, ValueError):
            return None
        if (
            isinstance(response_dict, dict)
            and "major" in response_dict
            and isinstance(response_dict["major"], int)
        ):
            return response_dict
    except Exception:
        pass
    return None


def _validate_braiins_password(
    ip: str,
    api_port: int,
    password: str,
    timeout: float,
    source_ip: str = "",
) -> bool:
    """Validate a candidate password against a fingerprinted Braiins OS miner.

    Braiins OS validates password via POST ``/api/v1/auth/login``. The
    username defaults to ``"root"`` (Braiins OS factory default) at scan
    time; operators can override per-miner via ``BRAIINS_USERNAME`` after
    registration. A successful response returns
    ``{token, timeout_s}`` — we only check for the presence of ``token``.

    NEVER raises. Returns False on any error path (probe-helper invariant).
    Does NOT log the password.
    """
    try:
        body = json.dumps({"username": "root", "password": password}).encode("utf-8")
        status, _headers, response_body = miner_http_request(
            ip,
            api_port,
            "/api/v1/auth/login",
            data=body,
            method="POST",
            timeout=timeout,
            source_ip=source_ip,
        )
        if status != 200:
            return False
        response_dict = json.loads(response_body.decode("utf-8", errors="replace"))
        return isinstance(response_dict, dict) and bool(response_dict.get("token"))
    except Exception:
        return False


def probe_miner(
    ip: str,
    *,
    source_ip: str,
    api_port: int,
    passwords: list[str],
    timeout: float,
) -> ProbeResult:
    """
    Probe a single IP for an ePIC-firmware, Whatsminer-firmware, Bixbit-firmware,
    LuxOS-firmware, or Braiins-firmware miner.

    Probe order (rationale: ePIC and Bixbit are the most common vendors in the
    target fleet; LuxOS and Braiins are less common but must coexist without
    ambiguity since each uses distinct fingerprints):

      1. GET /summary via HTTP — if 200 with 'Status.Operating State' present
         -> ePIC vendor_match=True; run ePIC password loop.
      2. If Step 1 didn't match (connection error, non-200, JSON parse failed,
         or no 'Status.Operating State'): probe for Whatsminer via TCP socket
         to (ip, api_port), send {"cmd":"get_token"}. If the response is a JSON
         dict whose "STATUS" field is "S", "Code" field is 133, "Msg" field is
         a dict containing "salt", "newsalt", and "time" -> Whatsminer fingerprint
         matched; store passwords[0] as password_found.
      3. If Step 2 didn't match (connection error, non-200, JSON parse failed,
         or no 'Status.Operating State'): open a TCP socket to (ip, api_port),
         send {"cmd":"summary"}. If the response is a JSON dict whose
         "STATUS" field is a string (e.g. "S" or "E") -> Bixbit fingerprint
         matched; store passwords[0] as password_found. (LuxOS error responses
         to this probe shape "STATUS" as a list-of-dicts and are rejected.)
      4. If Step 3 didn't match: probe for LuxOS via TCP socket to (ip, api_port),
         send {"command":"version"}. If the response is a JSON dict containing
         "STATUS" as list-of-dicts and "VERSION" as list with at least one element
         containing key "LUXminer" -> LuxOS fingerprint matched; store passwords[0]
         as password_found.
      5. If Step 4 didn't match: GET /api/v1/version/ via HTTP. If the response
         is a JSON dict with an integer 'major' field -> Braiins fingerprint
         matched; store passwords[0] as password_found.
      6. Vendor-match gate: no password attempts against IPs that matched
         none of the four fingerprints.

    On any vendor-match success path, also calls _resolve_mac_or_synth to
    populate ProbeResult.mac and id_synthesized — the persistent stable
    identifier the scanner uses to key per-miner state regardless of IP.
    For ePIC, Bixbit, and LuxOS the function tries the vendor API first
    (extracted via MinerSummary.from_<vendor>().mac); falls back to ARP
    when the vendor's response doesn't carry a MAC; falls back to a
    synthetic ID when neither is available. Braiins probes don't carry
    MAC at the unauthenticated /api/v1/version/ endpoint — the scanner
    runner fetches the MAC via /api/v1/miner/details after probe success.

    Args:
        ip: Target IPv4 address string.
        source_ip: source IP to bind outgoing probes to. "" means OS default.
                   Resolved once per scan by the caller; not auto-detected per-probe.
        api_port: ePIC/Bixbit/LuxOS API port (typically 4028). Braiins OS typically
                  runs on port 80; see _probe_braiins_http for the port tension note.
        passwords: List of passwords to try in order.
        timeout: Per-request timeout in seconds.

    Returns:
        ProbeResult — always populated, never raises.
    """
    try:
        # Step 1: reachability check via HTTP /summary
        try:
            status, _headers, body = miner_http_request(
                ip, api_port, "/summary", method="GET", timeout=timeout, source_ip=source_ip
            )
        except Exception:
            # HTTP probe failed — fall through to Whatsminer TCP probe
            status = None

        response_json = None
        vendor_match = False
        hostname = None

        if status == 200:
            # Parse JSON, check ePIC vendor fingerprint
            try:
                response_json = json.loads(body.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                # JSON parse failed — fall through to Whatsminer probe
                pass
            else:
                status_block = (
                    response_json.get("Status") if isinstance(response_json, dict) else None
                )
                vendor_match = (
                    isinstance(status_block, dict)
                    and status_block.get("Operating State") is not None
                )
                # ePIC PowerPlay-BMS v1.17.x returns Hostname at the top level
                # of /summary; older / newer variants may nest under Network.
                # Try both.
                if isinstance(response_json, dict):
                    hostname = response_json.get("Hostname")
                    if not hostname:
                        network_block = response_json.get("Network")
                        if isinstance(network_block, dict):
                            hostname = network_block.get("Hostname")

                if vendor_match:
                    # Fetch /network for canonical MAC. PowerPlay-BMS firmware
                    # exposes MAC at dhcp.mac_address (NOT in /summary).
                    # Wrapped in try/except so a malformed response doesn't
                    # propagate out of the vendor_match block.
                    epic_network = _fetch_epic_network(ip, api_port, timeout, source_ip)
                    try:
                        epic_vendor_mac = MinerSummary.from_epic(
                            response_json, raw_network=epic_network
                        ).mac
                    except Exception:
                        epic_vendor_mac = None
                    # ePIC fingerprint matched — try each password
                    for pw in passwords:
                        try:
                            payload = json.dumps({"password": pw, "param": {}}).encode("utf-8")
                            pw_status, _ph, pw_body = miner_http_request(
                                ip,
                                api_port,
                                "/get_voltage",
                                data=payload,
                                method="POST",
                                timeout=timeout,
                                source_ip=source_ip,
                            )
                            if pw_status == 200:
                                try:
                                    json.loads(pw_body.decode("utf-8", errors="replace"))
                                    mac, id_synthesized = _resolve_mac_or_synth(
                                        ip, source_ip, vendor_mac=epic_vendor_mac
                                    )
                                    return ProbeResult(
                                        ip=ip,
                                        reachable=True,
                                        vendor_match=True,
                                        password_found=pw,
                                        hostname=hostname,
                                        error=None,
                                        firmware_type="epic",
                                        summary_raw=response_json,
                                        mac=mac,
                                        id_synthesized=id_synthesized,
                                    )
                                except (json.JSONDecodeError, ValueError):
                                    continue
                        except Exception:
                            continue

                    # ePIC vendor matched but no password worked
                    mac, id_synthesized = _resolve_mac_or_synth(
                        ip, source_ip, vendor_mac=epic_vendor_mac
                    )
                    return ProbeResult(
                        ip=ip,
                        reachable=True,
                        vendor_match=True,
                        password_found=None,
                        hostname=hostname,
                        error=None,
                        firmware_type="epic",
                        summary_raw=response_json,
                        mac=mac,
                        id_synthesized=id_synthesized,
                    )

        # Step 2: Whatsminer TCP probe (ePIC path didn't match)
        whatsminer_response = _probe_whatsminer_tcp(ip, api_port, timeout, source_ip)
        if isinstance(whatsminer_response, dict):
            # Whatsminer fingerprint matched — try MAC from summary (typically
            # None per Whatsminer docs); fall back to ARP/synth via resolver.
            mac, id_synthesized = _resolve_mac_or_synth(ip, source_ip)
            # Iterate operator-configured passwords; store first that validates
            # against AES-encrypted {"cmd":"status"} probe. If none validate
            # (or salt is unavailable), password_found=None — vendor_match
            # stays True so the operator can rescan after fixing the password
            # list.
            password_found: str | None = None
            msg_block = whatsminer_response.get("Msg")
            salt = msg_block.get("salt") if isinstance(msg_block, dict) else None
            if salt and passwords:
                for pw in passwords:
                    if _validate_whatsminer_password(ip, api_port, pw, salt, timeout, source_ip):
                        password_found = pw
                        break
            return ProbeResult(
                ip=ip,
                reachable=True,
                vendor_match=True,
                password_found=password_found,
                hostname=None,
                error=None,
                firmware_type="whatsminer",
                summary_raw=whatsminer_response,
                mac=mac,
                id_synthesized=id_synthesized,
            )

        # Step 3: Bixbit TCP probe (ePIC and Whatsminer paths didn't match)
        bixbit_response = _probe_bixbit_tcp(ip, api_port, timeout, source_ip)
        if isinstance(bixbit_response, dict) and isinstance(bixbit_response.get("STATUS"), str):
            # Bixbit fingerprint matched — try MAC from summary (typically
            # None per Whatsminer docs); fall back to ARP/synth via resolver.
            # Iterate operator-configured passwords; store first that validates
            # via the Bixbit connectivity-check stub. If none validate,
            # password_found=None — vendor_match stays True so the operator
            # can rescan after fixing the password list. (Bixbit's plaintext
            # API does not currently validate passwords; see helper docstring.)
            try:
                bixbit_vendor_mac = MinerSummary.from_bixbit(bixbit_response).mac
            except Exception:
                bixbit_vendor_mac = None
            mac, id_synthesized = _resolve_mac_or_synth(ip, source_ip, vendor_mac=bixbit_vendor_mac)
            password_found: str | None = None
            for pw in passwords:
                if _validate_bixbit_password(ip, api_port, pw, timeout, source_ip):
                    password_found = pw
                    break
            return ProbeResult(
                ip=ip,
                reachable=True,
                vendor_match=True,
                password_found=password_found,
                hostname=None,
                error=None,
                firmware_type="bixbit",
                summary_raw=bixbit_response,
                mac=mac,
                id_synthesized=id_synthesized,
            )

        # Step 4: LuxOS TCP probe (ePIC, Whatsminer, and Bixbit paths didn't match)
        luxos_response = _probe_luxos_tcp(ip, api_port, timeout, source_ip)
        if isinstance(luxos_response, dict):
            # Check LuxOS fingerprint: STATUS must be list-of-dicts, VERSION[0] must have LUXminer
            lux_status = luxos_response.get("STATUS")
            lux_version = luxos_response.get("VERSION")
            if (
                isinstance(lux_status, list)
                and len(lux_status) > 0
                and isinstance(lux_status[0], dict)
                and isinstance(lux_version, list)
                and len(lux_version) > 0
                and "LUXminer" in lux_version[0]
            ):
                # LuxOS fingerprint matched — fetch config cmd to get MAC
                # (CONFIG[0]["MACAddr"] confirmed for LUXminer 2026.4.3).
                # Fall back to ARP/synth via resolver if config fails.
                luxos_config = _fetch_luxos_config(ip, api_port, timeout, source_ip)
                try:
                    luxos_vendor_mac = MinerSummary.from_luxos(
                        luxos_response, raw_config=luxos_config
                    ).mac
                except Exception:
                    luxos_vendor_mac = None
                mac, id_synthesized = _resolve_mac_or_synth(
                    ip, source_ip, vendor_mac=luxos_vendor_mac
                )
                # Iterate operator-configured passwords; store first that
                # validates via logon. If none validate, password_found=None —
                # vendor_match stays True so the operator can rescan after
                # fixing the password list. Each successful logon is followed
                # by a best-effort logoff in the helper to avoid leaking
                # sessions on the miner.
                password_found: str | None = None
                if passwords:
                    for pw in passwords:
                        if _validate_luxos_password(ip, api_port, pw, timeout, source_ip):
                            password_found = pw
                            break
                return ProbeResult(
                    ip=ip,
                    reachable=True,
                    vendor_match=True,
                    password_found=password_found,
                    hostname=None,
                    error=None,
                    firmware_type="luxos",
                    mac=mac,
                    id_synthesized=id_synthesized,
                )

        # Step 5: Braiins OS HTTP probe (ePIC, Whatsminer, Bixbit, and LuxOS paths didn't match)
        braiins_response = _probe_braiins_http(ip, api_port, timeout, source_ip)
        if (
            isinstance(braiins_response, dict)
            and "major" in braiins_response
            and isinstance(braiins_response["major"], int)
        ):
            # Braiins fingerprint matched — iterate operator-configured
            # passwords and store the first that validates via POST
            # /api/v1/auth/login. If none validate, password_found=None —
            # vendor_match stays True so the operator can rescan after
            # fixing the password list. MAC is NOT in the unauthenticated
            # /api/v1/version/ response; the scanner runner fetches it via
            # /api/v1/miner/details after probe success, using the validated
            # password to auth.
            mac, id_synthesized = _resolve_mac_or_synth(ip, source_ip)
            password_found: str | None = None
            for pw in passwords:
                if _validate_braiins_password(ip, api_port, pw, timeout, source_ip):
                    password_found = pw
                    break
            return ProbeResult(
                ip=ip,
                reachable=True,
                vendor_match=True,
                password_found=password_found,
                hostname=None,
                error=None,
                firmware_type="braiins",
                mac=mac,
                id_synthesized=id_synthesized,
            )

        # None of the five vendor fingerprints matched
        return ProbeResult(
            ip=ip,
            reachable=False,
            vendor_match=False,
            password_found=None,
            hostname=None,
            error="No vendor match",
            firmware_type=None,
            mac=None,
            id_synthesized=False,
        )

    except Exception as exc:
        return ProbeResult(
            ip=ip,
            reachable=False,
            vendor_match=False,
            password_found=None,
            hostname=None,
            error=str(exc),
            firmware_type=None,
            mac=None,
            id_synthesized=False,
        )
