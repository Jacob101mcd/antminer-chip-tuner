from __future__ import annotations

import mimetypes
import os
from importlib.resources import files
from importlib.resources.abc import Traversable

# Resolve the packaged asset tree through importlib.resources so the dashboard
# works from an installed wheel as well as from a source checkout.
STATIC_ROOT = files("tuner_app").joinpath("static")

# Whitelist of subtrees we allow under /static/*. Prevents path traversal
# from reaching anywhere outside static/. Each entry is a directory name.
STATIC_SUBDIRS = ("css", "js", "vendor")


def _send_file(
    handler,
    resource: Traversable | str | os.PathLike[str],
    content_type: str,
) -> None:
    """Read a packaged resource or filesystem path and write the HTTP response.

    JS and CSS responses carry ``Cache-Control: no-store, no-cache,
    must-revalidate`` so a tuner upgrade isn't masked by an operator's
    browser still running stale main.js / overview.css. Without this,
    operators upgrading the tuner can hit fix-already-deployed bugs
    indefinitely (verified post-PR-#54: the "0/0 succeeded" regression
    persisted on a deployed tuner because of cached pre-fix main.js).
    """
    if isinstance(resource, Traversable):
        content = resource.read_bytes()
    else:
        with open(resource, "rb") as file_obj:
            content = file_obj.read()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", len(content))
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
    if "text/html" in content_type.lower():
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        handler.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
    if "javascript" in content_type.lower() or "css" in content_type.lower():
        handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    handler.end_headers()
    handler.wfile.write(content)


def serve_dashboard(handler, body) -> None:
    """Handle GET / and /index.html by serving the packaged dashboard shell."""
    resource = STATIC_ROOT.joinpath("dashboard.html")
    try:
        _send_file(handler, resource, "text/html; charset=utf-8")
    except FileNotFoundError:
        handler.send_response(404)
        handler.end_headers()
        handler.wfile.write(b"dashboard.html not found")


def serve_static(handler, body) -> None:
    """Serve a packaged file under ``static/<css|js|vendor>/<path>``.

    The dispatcher in routes.py registers this as a prefix handler for
    "/static/". Path traversal is blocked: the request path must start with
    /static/<subdir>/ where subdir is in STATIC_SUBDIRS.
    """
    raw_path = handler.path.split("?", 1)[0]
    # Strip the leading "/static/" — the dispatcher already matched that prefix.
    if not raw_path.startswith("/static/"):
        handler.send_response(404)
        handler.end_headers()
        return
    rel = raw_path[len("/static/") :]

    # Reject any traversal-y components before joining.
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        handler.send_response(404)
        handler.end_headers()
        return
    parts = rel.split("/", 1)
    if parts[0] not in STATIC_SUBDIRS:
        handler.send_response(404)
        handler.end_headers()
        return

    resource = STATIC_ROOT.joinpath(*rel.split("/"))
    if not resource.is_file():
        handler.send_response(404)
        handler.end_headers()
        return

    # Pick a sensible MIME type. The stdlib `mimetypes` module is enough but
    # doesn't always know about modern web defaults — patch a few here.
    ctype, _ = mimetypes.guess_type(resource.name)
    if ctype is None:
        if resource.name.endswith(".js"):
            ctype = "application/javascript"
        elif resource.name.endswith(".css"):
            ctype = "text/css"
        elif resource.name.endswith(".html"):
            ctype = "text/html"
        else:
            ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in (
        "application/javascript",
        "application/json",
    ):
        ctype = ctype + "; charset=utf-8" if "charset" not in ctype else ctype

    try:
        _send_file(handler, resource, ctype)
    except FileNotFoundError:
        handler.send_response(404)
        handler.end_headers()
