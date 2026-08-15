"""The event bus (SRS §5.4).

One monotonic sequence per session. Subscribers detect gaps and ask for a
replay, which is what lets a laptop join a session a phone is recording, or
rejoin after a tunnel drop, without a full refetch (FR-SES-5).

Delivery is per-subscriber and bounded: a viewer on a slow link that stops
draining its queue is dropped rather than allowed to apply back-pressure to the
pipeline. Losing a viewer is recoverable; stalling the recording is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)

SUBSCRIBER_QUEUE_MAX = 512


class EventType(StrEnum):
    SESSION_START = "session.start"
    SESSION_END = "session.end"
    SESSION_MODE_CHANGED = "session.mode_changed"
    SESSION_PAUSED = "session.paused"
    SESSION_RESUMED = "session.resumed"
    UTTERANCE_PARTIAL = "utterance.partial"
    UTTERANCE_FINAL = "utterance.final"
    SPEAKER_CHANGED = "speaker.changed"
    TRANSLATION_FINAL = "translation.final"
    TRANSCRIPT_EDITED = "transcript.edited"
    ARTIFACT_CREATED = "artifact.created"
    CAPTURE_ERROR = "capture.error"
    PLUGIN_ERROR = "plugin.error"
    PLUGIN_STARTED = "plugin.started"
    STATUS = "status"  # ingest health: buffered ms, drops, cost


#: Types worth replaying to a reconnecting viewer. Partials are excluded — they
#: are superseded by their final, so replaying them would resurrect text the
#: viewer has already seen corrected.
REPLAYABLE = frozenset(EventType) - {EventType.UTTERANCE_PARTIAL, EventType.STATUS}


@dataclass(slots=True)
class Event:
    type: EventType
    session_id: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "type": str(self.type),
            "session_id": self.session_id,
            "seq": self.seq,
            "ts": self.ts,
            "data": self.data,
        }


class Subscriber:
    """A bounded queue with an overflow flag.

    On overflow we do not silently drop: the flag travels to the client, which
    responds by refetching the session. Silent loss in a transcript is worse
    than a visible reload.
    """

    __slots__ = ("_queue", "overflowed", "session_id")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self.overflowed = False

    def offer(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.overflowed = True

    def close(self) -> None:
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event


class EventBus:
    """Fan-out per session, with a durable sequence.

    The bus assigns `seq` and hands the event to an optional sink (the event log)
    before fanning out, so a replayed stream and a live stream carry identical
    numbering.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._seq: dict[str, int] = {}
        self._sink: Any = None  # set by the app: async (Event) -> None

    def set_sink(self, sink: Any) -> None:
        self._sink = sink

    def seed_seq(self, session_id: str, seq: int) -> None:
        """Continue numbering after a restart, so a viewer's `last_seq` still
        means what it meant before the process died."""
        self._seq[session_id] = max(self._seq.get(session_id, 0), seq)

    def subscribe(self, session_id: str) -> Subscriber:
        sub = Subscriber(session_id)
        self._subscribers.setdefault(session_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        peers = self._subscribers.get(sub.session_id)
        if peers is not None:
            peers.discard(sub)
            if not peers:
                self._subscribers.pop(sub.session_id, None)
        sub.close()

    def subscriber_count(self, session_id: str) -> int:
        return len(self._subscribers.get(session_id, ()))

    async def publish(
        self, session_id: str, type_: EventType, data: dict[str, Any] | None = None
    ) -> Event:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = Event(
            type=type_,
            session_id=session_id,
            data=data or {},
            seq=seq,
            ts=datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        if self._sink is not None and type_ in REPLAYABLE:
            try:
                await self._sink(event)
            except Exception:  # a full disk must not stop the live stream
                log.exception("event sink failed", extra={"event": str(type_)})
        for sub in tuple(self._subscribers.get(session_id, ())):
            sub.offer(event)
        return event

    def close_session(self, session_id: str) -> None:
        for sub in tuple(self._subscribers.pop(session_id, ())):
            sub.close()
        self._seq.pop(session_id, None)
