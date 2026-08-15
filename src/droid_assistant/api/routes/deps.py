"""Dependency accessors. The services container lives on `app.state`."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request, WebSocket

from ..services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services  # type: ignore[no-any-return]


def ws_services(websocket: WebSocket) -> Services:
    return websocket.app.state.services  # type: ignore[no-any-return]


#: The annotation every route uses. Naming the concrete type rather than `Any`
#: is what lets the type checker see into the handlers at all.
ServicesDep = Annotated[Services, Depends(get_services)]
