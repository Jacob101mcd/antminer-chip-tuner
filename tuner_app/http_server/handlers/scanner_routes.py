"""HTTP route handlers for the IP scanner."""

from __future__ import annotations

# Set by tuner_app.main at startup — mirrors how manager is exposed to handlers.
scanner = None


def scanner_status(handler, body) -> None:
    """Handle GET /tuner/scanner/status."""
    if scanner is None:
        handler._json_response({"ok": False, "error": "scanner not initialized"}, status=503)
        return
    handler._json_response(scanner.get_status())


def scanner_scan_now(handler, body) -> None:
    """Handle POST /tuner/scanner/scan_now."""
    if scanner is None:
        handler._json_response({"ok": False, "error": "scanner not initialized"}, status=503)
        return
    scanner.request_scan_now()
    handler._json_response({"ok": True})


def scanner_stop(handler, body) -> None:
    """Handle POST /tuner/scanner/stop."""
    if scanner is None:
        handler._json_response({"ok": False, "error": "scanner not initialized"}, status=503)
        return
    scanner.stop()
    handler._json_response({"ok": True})
