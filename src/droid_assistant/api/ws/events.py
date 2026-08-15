"""`WS /ws/sessions/{id}` — the server → client live event stream (SRS §5.4).

Recording and viewing are separate connections, so a phone records while a
laptop watches (FR-SES-5). A viewer supplies the last `seq` it received and the
server replays from there, or tells it to refetch when the gap is too large.

Backlog policy is deliberate: a viewer that stops draining is disconnected
rather than allowed to grow a queue. Losing a viewer costs a reconnect; stalling
the pipeline costs the recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ...events import EventType, Subscriber
from .origin import origin_allowed

log = logging.getLogger(__name__)
router = APIRouter()

#: Beyond this many missed events, replaying costs more than refetching.
MAX_REPLAY = 1000
HEARTBEAT_S = 25.0


@router.websocket("/ws/sessions/{session_id}")
async def session_events(websocket: WebSocket, session_id: str) -> None:
    services = websocket.app.state.services
    if not origin_allowed(websocket, services.settings):
        await websocket.close(code=4401, reason="origin not allowed")
        return

    record = await services.repo.get_session(session_id)
    if record is None:
        await websocket.accept()
        await websocket.close(code=4404, reason="no such session")
        return

    await websocket.accept()

    raw_last = websocket.query_params.get("last_seq")
    try:
        last_seq = int(raw_last) if raw_last is not None else 0
    except ValueError:
        last_seq = 0

    current = await services.repo.max_event_seq(session_id)
    services.bus.seed_seq(session_id, current)

    # Subscribe *before* replaying, so an event published during the replay is
    # queued rather than lost in the gap between the two.
    subscriber = services.bus.subscribe(session_id)
    try:
        if last_seq and current - last_seq > MAX_REPLAY:
            await websocket.send_json(
                {
                    "type": "resync",
                    "reason": f"{current - last_seq} events missed; refetch the session",
                    "session_id": session_id,
                    "seq": current,
                }
            )
        else:
            for payload in await services.repo.replay_events(session_id, last_seq):
                await websocket.send_json(payload)

        await websocket.send_json(
            {
                "type": "subscribed",
                "session_id": session_id,
                "seq": current,
                "live": session_id in services.sessions.active_ids,
                "status": services.sessions.status(session_id),
            }
        )

        await _pump(websocket, subscriber, session_id)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("event stream failed", extra={"session": session_id})
        if websocket.client_state is WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
    finally:
        services.bus.unsubscribe(subscriber)


async def _pump(websocket: WebSocket, subscriber: Subscriber, session_id: str) -> None:
    """Forward events, with a heartbeat so an idle tunnel is not reaped."""
    reader = asyncio.create_task(_drain_client(websocket))
    try:
        iterator = subscriber.__aiter__()
        while True:
            try:
                event = await asyncio.wait_for(iterator.__anext__(), timeout=HEARTBEAT_S)
            except TimeoutError:
                await websocket.send_json({"type": "heartbeat", "session_id": session_id})
                continue
            except StopAsyncIteration:
                return

            if subscriber.overflowed:
                await websocket.send_json(
                    {
                        "type": "resync",
                        "reason": "this viewer fell behind; refetch the session",
                        "session_id": session_id,
                        "seq": event.seq,
                    }
                )
                return
            await websocket.send_json(event.to_json())
            if event.type is EventType.SESSION_END:
                # Stay connected: post-processing artifacts still arrive after
                # the session ends, and FR-UI-17 needs the client to see them.
                continue
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


async def _drain_client(websocket: WebSocket) -> None:
    """Consume anything the viewer sends.

    Viewers are read-only, but a socket whose receive side is never read cannot
    observe the peer closing, which leaks a subscriber per abandoned tab.
    """
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await websocket.receive()
