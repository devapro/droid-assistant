"""WebSocket origin validation (NFR-SEC-4).

A browser attaches `Origin` to a WebSocket handshake but does not enforce the
same-origin policy on it, so a page on another site can open a socket to this
server if the network reaches it. When bound to loopback that is not
interesting; when bound to a LAN address or reached over Tailscale, it is, so
the check is applied exactly there.
"""

from __future__ import annotations

import logging

from fastapi import WebSocket

from ...config import Settings

log = logging.getLogger(__name__)

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def origin_allowed(websocket: WebSocket, settings: Settings) -> bool:
    if settings.server.host in LOOPBACK:
        return True  # only this machine can reach us at all

    origin = websocket.headers.get("origin")
    if origin is None:
        # Non-browser clients (the CLI, tests, a script) send no Origin. They
        # still need a valid ingest token, which is the actual access control.
        return True

    allowed = set(settings.server.allowed_origins)
    host = websocket.headers.get("host")
    if host:
        allowed |= {f"https://{host}", f"http://{host}"}

    if origin in allowed:
        return True
    log.warning("rejected websocket origin", extra={"origin": origin, "host": host})
    return False
