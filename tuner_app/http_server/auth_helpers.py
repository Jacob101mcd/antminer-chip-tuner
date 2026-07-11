from __future__ import annotations

import http.cookies
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

from tuner_app.auth.sessions import validate_session
from tuner_app.constants import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SEC,
)

# Paths reachable without a valid session. Everything else is gated.
# "/" and "/index.html" serve the dashboard HTML which itself contains the
# login view, so unauthenticated users must be able to fetch it. Phase 7 split
# the dashboard into a shell + /static/* asset tree (CSS, JS, vendored
# Chart.js); the browser fetches those before the operator has a session, so
# the /static/ prefix needs the same exemption.
AUTH_EXEMPT_GET_PATHS = {"/", "/index.html", "/tuner/auth/status", "/tuner/firmware_types"}
AUTH_EXEMPT_GET_PREFIXES = ("/static/",)
AUTH_EXEMPT_POST_PATHS = {"/tuner/login", "/tuner/setup"}

TRUSTED_PROXIES_ENV = "ANTMINER_TUNER_TRUSTED_PROXIES"
ALLOWED_HOSTS_ENV = "ANTMINER_TUNER_ALLOWED_HOSTS"
SECURE_COOKIES_ENV = "ANTMINER_TUNER_SECURE_COOKIES"

_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.IGNORECASE)
_MAX_FORWARDED_HEADER_BYTES = 4096
_MAX_FORWARDED_HOPS = 32


def _header_values(handler, name: str) -> tuple[str, ...]:
    """Return every occurrence of a request header without merging duplicates."""
    get_all = getattr(handler.headers, "get_all", None)
    if callable(get_all):
        values = get_all(name)
        if values is not None:
            return tuple(str(value) for value in values)
    value = handler.headers.get(name)
    return () if value is None else (str(value),)


def _single_header_value(handler, name: str) -> str | None:
    values = _header_values(handler, name)
    if len(values) != 1:
        return None
    return values[0]


def _valid_dns_name(value: str) -> bool:
    if not value or len(value) > 253 or value.endswith("."):
        return False
    labels = value.split(".")
    return all(_DNS_LABEL_RE.fullmatch(label) is not None for label in labels)


def _parse_host_authority(
    value: str,
) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None] | None:
    """Parse a Host authority into its normalized host and optional IP literal."""
    authority = value.strip()
    if (
        not authority
        or any(ord(char) < 0x21 or ord(char) == 0x7F for char in authority)
        or any(char in authority for char in "/\\?#@,")
    ):
        return None

    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0:
            return None
        literal = authority[1:closing]
        remainder = authority[closing + 1 :]
        if remainder and (not remainder.startswith(":") or not _valid_port(remainder[1:])):
            return None
        try:
            parsed = ipaddress.ip_address(literal)
        except ValueError:
            return None
        if parsed.version != 6:
            return None
        return str(parsed), parsed

    if authority.count(":") > 1:
        # IPv6 literals must use brackets in an HTTP Host header.
        return None
    if ":" in authority:
        host, port = authority.rsplit(":", 1)
        if not host or not _valid_port(port):
            return None
    else:
        host = authority

    normalized = host.casefold()
    try:
        parsed = ipaddress.ip_address(normalized)
    except ValueError:
        parsed = None
    if parsed is None and not _valid_dns_name(normalized):
        return None
    return normalized, parsed


def _valid_port(value: str) -> bool:
    if not value or not value.isascii() or not value.isdigit():
        return False
    # Compare as an integer only after bounding the digit count.
    return len(value) <= 5 and int(value) <= 65535


def _allowed_dns_hosts() -> frozenset[str]:
    """Return exact DNS names explicitly allowed for the request Host header."""
    allowed = set()
    for item in os.getenv(ALLOWED_HOSTS_ENV, "").split(","):
        normalized = item.strip().casefold()
        if _valid_dns_name(normalized):
            allowed.add(normalized)
    return frozenset(allowed)


def _request_host(handler):
    raw = _single_header_value(handler, "Host")
    if raw is None:
        return None
    return _parse_host_authority(raw)


def is_allowed_host(handler) -> bool:
    """Allow localhost, private/loopback IP literals, or explicit exact DNS names."""
    parsed_host = _request_host(handler)
    if parsed_host is None:
        return False
    hostname, address = parsed_host
    if hostname == "localhost":
        return True
    if address is not None:
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return address.is_loopback or address.is_private
    return hostname in _allowed_dns_hosts()


def is_loopback_host(handler) -> bool:
    """Return whether Host names localhost or contains a loopback IP literal."""
    parsed_host = _request_host(handler)
    if parsed_host is None:
        return False
    hostname, address = parsed_host
    if hostname == "localhost":
        return True
    if address is None:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def require_valid_host(handler) -> bool:
    """Reject unrecognized Host values to block DNS-rebinding requests."""
    if is_allowed_host(handler):
        return True
    body = json.dumps({"ok": False, "error": "invalid host"}).encode()
    handler.close_connection = True
    handler.send_response(400)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)
    return False


def _trusted_proxy_networks():
    """Parse the explicit comma-separated trusted proxy IP/CIDR allowlist."""
    raw = os.getenv(TRUSTED_PROXIES_ENV, "")
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # A typo must reduce trust, never broaden it.
            continue
    return tuple(networks)


def _parse_ip(value: object):
    text = str(value).strip()
    if "%" in text:
        # Scoped IPv6 identifiers are not valid forwarding identities.
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _is_trusted_proxy(address, networks) -> bool:
    parsed = _parse_ip(address)
    return parsed is not None and any(parsed in network for network in networks)


def get_client_ip(handler) -> str:
    """Return the client IP, trusting forwarding data only from allowlisted peers."""
    peer = (handler.client_address[0] if handler.client_address else "") or ""
    networks = _trusted_proxy_networks()
    if not networks or not _is_trusted_proxy(peer, networks):
        return peer

    xff = _single_header_value(handler, "X-Forwarded-For")
    if not xff or len(xff) > _MAX_FORWARDED_HEADER_BYTES:
        return peer
    items = xff.split(",")
    if len(items) > _MAX_FORWARDED_HOPS:
        return peer
    forwarded = []
    for item in items:
        parsed = _parse_ip(item)
        if parsed is None:
            return peer
        forwarded.append(str(parsed))
    if not forwarded:
        return peer

    # Walk right-to-left through trusted hops.  This prevents a caller from
    # spoofing the leftmost value when only the immediate reverse proxy is
    # trusted, while supporting explicitly trusted multi-proxy chains.
    current = peer
    for candidate in reversed(forwarded):
        if not _is_trusted_proxy(current, networks):
            break
        current = candidate
    return current


def is_loopback_client(handler) -> bool:
    """Return whether the authenticated network identity is local loopback."""
    parsed = _parse_ip(get_client_ip(handler))
    if parsed is None:
        return False
    if parsed.version == 6 and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_loopback


def _same_origin(handler, origin: str) -> bool:
    """Validate a browser Origin against the request Host authority."""
    try:
        parsed = urlsplit(origin)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.netloc or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.path or parsed.query or parsed.fragment:
            return False
        host = _single_header_value(handler, "Host")
        if host is None:
            return False
        host = host.strip()
        return bool(host) and parsed.netloc.casefold() == host.casefold()
    except (TypeError, ValueError):
        return False


def require_valid_post_origin(handler) -> bool:
    """Reject cross-origin browser POSTs while allowing non-browser clients."""
    origins = _header_values(handler, "Origin")
    if not origins:
        return True
    if len(origins) == 1 and _same_origin(handler, origins[0].strip()):
        return True
    body = json.dumps({"ok": False, "error": "forbidden origin"}).encode()
    handler.close_connection = True
    handler.send_response(403)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)
    return False


def get_session_token(handler) -> str | None:
    """Parse the session cookie value from the Cookie header, or None."""
    raw = _single_header_value(handler, "Cookie")
    if not raw:
        return None
    try:
        jar = http.cookies.SimpleCookie()
        jar.load(raw)
        m = jar.get(SESSION_COOKIE_NAME)
        return m.value if m else None
    except Exception:
        return None


def set_session_cookie(handler, token: str) -> None:
    """Emit the Set-Cookie header for a freshly issued session token."""
    secure = "; Secure" if os.getenv(SECURE_COOKIES_ENV, "").strip() == "1" else ""
    handler.send_header(
        "Set-Cookie",
        f"{SESSION_COOKIE_NAME}={token}; HttpOnly; Path=/; Max-Age={SESSION_TTL_SEC}; "
        f"SameSite=Strict{secure}",
    )


def clear_session_cookie(handler) -> None:
    """Emit a Set-Cookie header that expires the session cookie."""
    secure = "; Secure" if os.getenv(SECURE_COOKIES_ENV, "").strip() == "1" else ""
    handler.send_header(
        "Set-Cookie",
        f"{SESSION_COOKIE_NAME}=; HttpOnly; Path=/; Max-Age=0; SameSite=Strict{secure}",
    )


def send_unauthenticated(handler) -> None:
    """Send a 401 JSON response with the standard unauthenticated body."""
    body = json.dumps({"ok": False, "error": "unauthenticated"}).encode()
    handler.close_connection = True
    handler.send_response(401)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", len(body))
    handler.end_headers()
    handler.wfile.write(body)


def require_auth(handler, method: str) -> bool:
    """Return True if the request is allowed; on False, 401 has already been sent."""
    path = handler.path.split("?", 1)[0]
    exempt = AUTH_EXEMPT_GET_PATHS if method == "GET" else AUTH_EXEMPT_POST_PATHS
    if path in exempt:
        return True
    if method == "GET":
        for prefix in AUTH_EXEMPT_GET_PREFIXES:
            if path.startswith(prefix):
                return True
    if validate_session(get_session_token(handler)):
        return True
    send_unauthenticated(handler)
    return False
