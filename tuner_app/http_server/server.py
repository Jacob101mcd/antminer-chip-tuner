from __future__ import annotations

import http.server
import socketserver


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTPServer; one daemon thread per request."""

    daemon_threads = True


def start_http_server(host: str, port: int, handler_class, manager) -> ThreadedHTTPServer:
    """Construct a ThreadedHTTPServer, attach manager to handler_class, return the server.

    The caller invokes serve_forever(); this lets tests create a server, run
    serve_forever in a thread, and shut it down cleanly after.
    """
    handler_class.manager = manager
    server = ThreadedHTTPServer((host, port), handler_class)
    return server
