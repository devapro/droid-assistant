"""`WS /ws/ingest` — the client → server audio upload (SRS §5.3).

The protocol alternates a JSON control frame and its binary payload:

    {"seq": 412, "t_ms": 8240, "codec": "pcm_s16le_16k", "samples": 3200}
    <6400 bytes of little-endian int16>

and the server acknowledges the highest *contiguous* sequence it holds:

    {"type": "ack", "through_seq": 412, "buffered_ms": 0}

"Contiguous" is the load-bearing word. The client keeps everything above that
number and retransmits from the first gap on reconnection, which is what makes
FR-CAP-6 and NFR-REL-3 achievable rather than aspirational. Acknowledging the
highest *received* number instead would let the client discard audio covering a
hole it never noticed.

Connections are token-gated (NFR-SEC-3) and origin-checked when the server is
not on loopback (NFR-SEC-4).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from ...domain import SAMPLE_RATE
from ...events import EventType
from ..services import SessionError
from .origin import origin_allowed

log = logging.getLogger(__name__)
router = APIRouter()

#: How far ahead of the contiguous point we will hold out-of-order chunks before
#: giving up on the gap. At 200 ms per chunk this is ~50 s of reordering, far
#: beyond anything a TCP-based transport produces; it exists to bound memory.
MAX_PENDING_CHUNKS = 256

CLOSE_UNAUTHORISED = 4401
CLOSE_GONE = 4404
CLOSE_PROTOCOL = 4400


class IngestSession:
    """Sequence tracking for one ingest connection."""

    def __init__(self) -> None:
        self.through_seq = -1
        self._pending: dict[int, np.ndarray] = {}
        self.received = 0
        self.duplicates = 0

    def offer(self, seq: int, samples: np.ndarray) -> list[np.ndarray]:
        """Accept a chunk; return whatever is now contiguous, in order."""
        self.received += 1
        if seq <= self.through_seq:
            self.duplicates += 1  # a retransmit of something already applied
            return []
        self._pending[seq] = samples

        ready: list[np.ndarray] = []
        while (chunk := self._pending.pop(self.through_seq + 1, None)) is not None:
            self.through_seq += 1
            ready.append(chunk)

        if len(self._pending) > MAX_PENDING_CHUNKS:
            # The gap is not going to be filled. Skip to the oldest thing we
            # hold rather than stalling the transcript indefinitely; the loss is
            # counted and shown.
            oldest = min(self._pending)
            log.warning(
                "ingest gap abandoned",
                extra={"expected": self.through_seq + 1, "skipping_to": oldest},
            )
            self.through_seq = oldest - 1
            while (chunk := self._pending.pop(self.through_seq + 1, None)) is not None:
                self.through_seq += 1
                ready.append(chunk)
        return ready

    @property
    def pending_count(self) -> int:
        return len(self._pending)


def decode(payload: bytes, codec: str) -> np.ndarray:
    if codec in {"pcm_s16le_16k", "pcm_s16le"}:
        return np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    if codec == "pcm_f32le":
        return np.frombuffer(payload, dtype="<f4").astype(np.float32)
    raise ValueError(
        f"unsupported codec {codec!r}; this endpoint accepts pcm_s16le_16k or pcm_f32le. "
        "Compressed transport is negotiated per session, not per chunk."
    )


@router.websocket("/ws/ingest")
async def ingest(websocket: WebSocket) -> None:
    services = websocket.app.state.services
    settings = services.settings

    if not origin_allowed(websocket, settings):
        await websocket.close(code=CLOSE_UNAUTHORISED, reason="origin not allowed")
        return

    token = websocket.query_params.get("token")
    session_id = await services.repo.resolve_token(token) if token else None
    if session_id is None:
        await websocket.accept()
        await websocket.close(code=CLOSE_UNAUTHORISED, reason="invalid or expired ingest token")
        return

    try:
        active = await services.sessions.attach_ingest(session_id)
    except SessionError as exc:
        await websocket.accept()
        await websocket.close(code=CLOSE_GONE, reason=str(exc)[:120])
        return

    await websocket.accept()
    # Sequence state lives on the session, so a reconnect continues where the
    # previous socket stopped rather than expecting sequence 0 again.
    if active.ingest_state is None:
        active.ingest_state = IngestSession()
    state: IngestSession = active.ingest_state
    pipeline = active.pipeline
    control: dict[str, Any] | None = None

    await websocket.send_json(
        {
            "type": "ready",
            "session_id": session_id,
            "chunk_ms": settings.capture.chunk_ms,
            "sample_rate": SAMPLE_RATE,
            "resume_from_seq": state.through_seq + 1,
        }
    )
    await services.bus.publish(session_id, EventType.STATUS, {"ingest_connected": True})

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if (text := message.get("text")) is not None:
                frame = json.loads(text)
                if frame.get("type") == "control":
                    await _handle_control(websocket, services, session_id, frame)
                    continue
                control = frame
                continue

            payload = message.get("bytes")
            if payload is None:
                continue
            if control is None:
                await websocket.close(
                    code=CLOSE_PROTOCOL, reason="binary frame arrived before its control frame"
                )
                return

            try:
                samples = decode(payload, control.get("codec", "pcm_s16le_16k"))
            except ValueError as exc:
                await websocket.close(code=CLOSE_PROTOCOL, reason=str(exc)[:120])
                return

            seq = int(control.get("seq", state.through_seq + 1))
            t_ms = control.get("t_ms")
            control = None

            for chunk in state.offer(seq, samples):
                await pipeline.push(chunk, t_ms if isinstance(t_ms, int) else None)

            status = pipeline.status()
            await websocket.send_json(
                {
                    "type": "ack",
                    "through_seq": state.through_seq,
                    "buffered_ms": status["buffered_ms"],
                    "pending": state.pending_count,
                    "dropped_ms": status["dropped_ms"],
                    "paused": status["paused"],
                }
            )
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ingest connection failed", extra={"session": session_id})
        if websocket.client_state is WebSocketState.CONNECTED:
            await websocket.close(code=1011, reason="server error")
    finally:
        log.info(
            "ingest closed",
            extra={
                "session": session_id,
                "chunks": state.received,
                "duplicates": state.duplicates,
                "through_seq": state.through_seq,
            },
        )
        await services.sessions.detach_ingest(session_id)


async def _handle_control(
    websocket: WebSocket, services: Any, session_id: str, frame: dict[str, Any]
) -> None:
    """In-band control, so pause and mark need no second round trip."""
    action = frame.get("action")
    active = services.sessions.get(session_id)
    if active is None:
        return
    match action:
        case "pause":
            await active.pipeline.pause()
        case "resume":
            await active.pipeline.resume()
        case "mark":
            # FR-CAP-18: flag the utterance in progress. Recorded against the
            # current ingest position; the session view resolves it to whichever
            # utterance covers that moment.
            await services.bus.publish(
                session_id,
                EventType.STATUS,
                {"mark_at_ms": active.pipeline.status().get("buffered_ms", 0), "marked": True},
            )
            await _mark_nearest(services, session_id, frame.get("t_ms"))
        case _:
            return
    await websocket.send_json({"type": "control_ack", "action": action})


async def _mark_nearest(services: Any, session_id: str, t_ms: int | None) -> None:
    if t_ms is None:
        return
    row = await services.db.fetch_one(
        "SELECT id FROM utterances WHERE session_id = ? ORDER BY abs(start_ms - ?) LIMIT 1",
        (session_id, t_ms),
    )
    if row is None:
        return
    await services.repo.update_utterance(row["id"], marked=True)
    # Told to every view watching this session, not just recorded. Without it
    # the button raised a toast and changed nothing anyone could see, which is
    # most of why the feature read as missing.
    await services.bus.publish(
        session_id,
        EventType.UTTERANCE_MARKED,
        {"utterance_id": row["id"], "marked": True},
    )
