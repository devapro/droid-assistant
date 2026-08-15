"""The FastAPI application.

Serves the API, the WebSocket endpoints, and the built client from one process,
so deployment is one container and one port (SRS §6.2).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings
from ..config import load as load_settings
from .routes import health, plugins, sessions
from .services import Services
from .ws import events as ws_events
from .ws import ingest as ws_ingest

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: NFR-SEC-9. No inline script, connections restricted to our own origin.
#: `'wasm-unsafe-eval'` is present because ONNX Runtime Web would need it if a
#: future client-side model ships; nothing today relies on it.
CSP = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "  # Tailwind emits a style attribute for transitions
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self' ws: wss:; "
    "worker-src 'self' blob:; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def create_app(settings: Settings | None = None, *, services: Services | None = None) -> FastAPI:
    """Build the app. `services` is injected by tests; production builds its own."""
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_services = services is None
        container = services or await Services.create(resolved)
        app.state.services = container
        if owns_services:
            await container.start()
        try:
            yield
        finally:
            if owns_services:
                await container.shutdown()

    app = FastAPI(
        title="droid-assistant",
        version=__version__,
        summary="Self-hosted conversation intelligence",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.include_router(health.router)
    app.include_router(sessions.router)
    app.include_router(plugins.router)
    app.include_router(ws_ingest.router)
    app.include_router(ws_events.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("content-security-policy", CSP)
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("referrer-policy", "same-origin")
        response.headers.setdefault("permissions-policy", "microphone=(self)")
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        """Errors name the component and suggest a remedy (FR-UI-9)."""
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.detail,
                "component": _component_for(request.url.path),
                "remedy": _REMEDIES.get(exc.status_code),
            },
            headers=getattr(exc, "headers", None),
        )

    _mount_client(app)
    return app


def _component_for(path: str) -> str:
    for prefix, name in (
        ("/api/sessions", "sessions"),
        ("/api/plugins", "plugins"),
        ("/api/search", "search"),
        ("/api/config", "configuration"),
        ("/ws/ingest", "audio ingest"),
    ):
        if path.startswith(prefix):
            return name
    return "server"


_REMEDIES = {
    404: "check the identifier, or reload the session list",
    409: "the session is not in a state that allows this — reload and try again",
    422: "the value was rejected by validation; the message names the field",
    501: "this is a planned feature that has not shipped yet",
    502: "an upstream backend failed; check Settings → Backends",
}


def _mount_client(app: FastAPI) -> None:
    """Serve the built client, with SPA fallback.

    A missing build is not an error: the API is fully usable without it, and the
    placeholder says how to build it rather than returning a bare 404.
    """
    index = STATIC_DIR / "index.html"
    if not index.exists():

        @app.get("/", include_in_schema=False, response_model=None)
        async def missing_client() -> JSONResponse:
            return JSONResponse(
                status_code=200,
                content={
                    "message": "the web client is not built",
                    "build_it": "cd web && npm install && npm run build",
                    "api_docs": "/api/docs",
                },
            )

        return

    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa(path: str) -> FileResponse:
        # Serve real files (manifest, service worker, icons) directly; everything
        # else falls through to index.html so client-side routing works on reload.
        candidate = (STATIC_DIR / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(STATIC_DIR.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)
