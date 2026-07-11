from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from tuner_app import state
from tuner_app.auth.lockout import (
    is_login_blocked,
    record_login_failure,
    record_login_success,
)
from tuner_app.auth.passwords import hash_password, verify_password
from tuner_app.auth.sessions import (
    issue_session,
    revoke_session,
    validate_session,
)
from tuner_app.config.persistence import save_config_to_disk
from tuner_app.http_server.auth_helpers import (
    clear_session_cookie,
    get_client_ip,
    get_session_token,
    is_loopback_client,
    is_loopback_host,
    set_session_cookie,
)


def auth_is_configured() -> bool:
    """Return True iff a password hash is currently stored."""
    with state.config_lock:
        return bool(state.AUTH.get("password_hash"))


def auth_status(handler, body) -> None:
    """Handle GET /tuner/auth/status."""
    configured = auth_is_configured()
    authenticated = False
    if configured:
        authenticated = validate_session(get_session_token(handler))
    handler._json_response({"authenticated": authenticated, "setup_required": not configured})


def login(handler, body) -> None:
    """Handle POST /tuner/login."""
    client_ip = get_client_ip(handler)
    if is_login_blocked(client_ip):
        time.sleep(2)
        handler._json_response({"ok": False, "error": "rate limited"}, status=429)
        return

    data = json.loads(body) if body else {}
    password = data.get("password", "")

    with state.config_lock:
        stored = state.AUTH.get("password_hash")
    if not stored:
        handler._json_response({"ok": False, "error": "setup_required"}, status=401)
        return

    if not verify_password(password, stored):
        record_login_failure(client_ip)
        time.sleep(0.2)
        handler._json_response({"ok": False, "error": "invalid password"}, status=401)
        return

    record_login_success(client_ip)
    token = issue_session()
    resp = json.dumps({"ok": True}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", len(resp))
    set_session_cookie(handler, token)
    handler.end_headers()
    handler.wfile.write(resp)


def logout(handler, body) -> None:
    """Handle POST /tuner/logout."""
    token = get_session_token(handler)
    revoke_session(token)
    resp = json.dumps({"ok": True}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", len(resp))
    clear_session_cookie(handler)
    handler.end_headers()
    handler.wfile.write(resp)


def setup(handler, body) -> None:
    """Handle POST /tuner/setup."""
    if not is_loopback_client(handler) or not is_loopback_host(handler):
        handler._json_response(
            {
                "ok": False,
                "error": "initial setup is restricted to loopback (client and Host required)",
            },
            status=403,
        )
        return

    data = json.loads(body) if body else {}
    password = data.get("password", "")

    if not isinstance(password, str) or len(password) < 12:
        handler._json_response(
            {"ok": False, "error": "password must be at least 12 characters"},
            status=400,
        )
        return

    with state.config_lock:
        if state.AUTH.get("password_hash"):
            # Re-check under lock to defeat double-submit race.
            handler._json_response(
                {"ok": False, "error": "already configured"},
                status=400,
            )
            return
        state.AUTH["password_hash"] = hash_password(password)
        state.AUTH["created_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        save_config_to_disk()

    token = issue_session()
    resp = json.dumps({"ok": True}).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", len(resp))
    set_session_cookie(handler, token)
    handler.end_headers()
    handler.wfile.write(resp)
