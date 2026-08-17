"""The plugin API (SRS §5.5). This module is the contract.

A plugin subclasses `Plugin`, declares which events it wants, and implements the
matching handlers. The host supplies a `Context` carrying read-only store
access, a pre-configured LLM client, validated config, and a namespaced logger.

Three properties of this design are deliberate:

* **Plugins never handle credentials** (FR-PLG-8). `ctx.llm` is already
  configured, already budget-aware, and already respects local-only. A plugin
  that constructed its own client would bypass all three.
* **Config is a pydantic model** (FR-PLG-5), so the settings form is generated
  from the schema rather than written twice.
* **`api_version` is checked on load** (NFR-MNT-2). A plugin built against a
  different major version is refused with a message, not loaded and crashed.

What this API does *not* do is isolate a plugin from the server. Plugins run
in-process with full privileges: installing one is equivalent to running
arbitrary code on the server (NFR-SEC-6, R11). That is documented rather than
solved in v1.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel

from ..domain import Artifact, Speaker, Utterance

__all__ = [
    "Artifact",
    "Context",
    "Event",
    "Plugin",
    "PluginStore",
    "Prompt",
    "Speaker",
    "Utterance",
]


@dataclass(slots=True, frozen=True)
class Prompt:
    """A saved instruction the operator picked for this run (FR-PLG-14).

    Held apart from `config` because the two answer different questions. Config
    is how a plugin is set up — one value, changed rarely, applying to every run.
    A prompt is what this particular artifact should be: several exist at once,
    and which one is right depends on the recording in front of you.
    """

    id: str
    name: str
    instructions: str


class Event(StrEnum):
    """Events a plugin can subscribe to (FR-PLG-2).

    Deliberately a subset of the wire event stream: `utterance.partial` is not
    offered, because a plugin acting on text that may still be rewritten is a
    bug generator, and `capture.error` is the server's business.
    """

    SESSION_START = "session.start"
    SESSION_END = "session.end"
    UTTERANCE_FINAL = "utterance.final"
    TRANSLATION_FINAL = "translation.final"
    SPEAKER_CHANGED = "speaker.changed"
    TRANSCRIPT_EDITED = "transcript.edited"


class PluginStore(Protocol):
    """Read-only view of a session. A plugin cannot mutate the transcript."""

    async def transcript(
        self, session_id: str, *, include_speakers: bool = True, include_translation: bool = False
    ) -> str:
        """The session as text, one line per utterance."""
        ...

    async def utterances(self, session_id: str) -> list[Utterance]: ...

    async def speakers(self, session_id: str) -> list[Speaker]: ...

    async def session_metadata(self, session_id: str) -> dict[str, Any]: ...

    async def artifacts(self, session_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        """The current artifacts for this session, newest first.

        A plugin reading its own last output is what makes a scoped run able to
        *add* to a list rather than replace it — see `Context.utterance_ids`.
        It is a read like any other: the transcript stays immutable to plugins.
        """
        ...


class LLM(Protocol):
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str: ...

    @property
    def available(self) -> bool: ...


@dataclass(slots=True)
class Context:
    """Everything a handler is given. Constructed by the host, never by a plugin."""

    session_id: str
    store: PluginStore
    llm: LLM
    config: Any  # a validated instance of the plugin's own `config_schema`
    logger: logging.Logger
    event: Event
    payload: dict[str, Any]

    #: The saved prompt this run was asked for, or None for the plugin's own
    #: instructions (FR-PLG-14). A plugin that declares `accepts_prompt` reads it
    #: and writes to it in place of whatever it would have said itself; one that
    #: does not is never handed one, because a control that changed nothing would
    #: be worse than no control.
    prompt: Prompt | None = None

    #: The part of the session this run covers, or None for all of it. Set when
    #: the operator asked for one message rather than the whole conversation —
    #: "make an action item out of *this*". A plugin honours it by reading
    #: `ctx.utterances()` instead of `ctx.store.utterances()`; one that reaches
    #: past it produces a whole-session answer to a question about one line.
    utterance_ids: tuple[str, ...] | None = None

    #: Why this run produced nothing, when it chose not to. Read back by the
    #: caller so "nothing happened" can be explained instead of merely reported.
    declined: str | None = None

    #: Set by the host; a plugin calls this to emit at any point, not only from
    #: `on_session_end` (SRS §5.5).
    _emit: Any = None

    def decline(self, reason: str) -> None:
        """Say why this run is producing nothing.

        Returning `None` is a complete answer to the host and a useless one to
        whoever pressed the button: "the plugin produced no artifact" tells them
        nothing they can act on. A reason does.
        """
        self.declined = reason

    @property
    def requested(self) -> bool:
        """True when a person asked for this run, rather than an event causing it.

        Worth checking before declining on a heuristic. A guard that skips work
        nobody asked for is right; the same guard applied to an explicit request
        is a button that does nothing.
        """
        return self.payload.get("reason") == "manual"

    async def emit_artifact(self, artifact: Artifact) -> None:
        if self._emit is None:
            raise RuntimeError("emit_artifact is unavailable outside a plugin handler")
        await self._emit(artifact)

    async def utterances(self) -> list[Utterance]:
        """The utterances this run covers, in order.

        The whole session unless it was scoped to part of one. Preferred over
        `store.utterances` for exactly that reason.
        """
        utterances = await self.store.utterances(self.session_id)
        if self.utterance_ids is None:
            return utterances
        wanted = set(self.utterance_ids)
        return [utterance for utterance in utterances if utterance.id in wanted]

    async def previous(self, kind: str) -> dict[str, Any] | None:
        """This plugin's current artifact of one kind, if it has produced one.

        What a scoped run adds to. None means there is nothing to add to yet,
        which a plugin should treat as "start the list", not as an error.
        """
        for artifact in await self.store.artifacts(self.session_id, kind=kind):
            if artifact.get("current", True):
                return artifact
        return None


class EmptyConfig(BaseModel):
    """Default schema for a plugin that needs no configuration."""


class Plugin:
    """Base class for plugins.

    Subclasses set the class attributes and implement the handlers for the
    events in `subscribes`. Handlers are async, run in a supervised task with a
    timeout (FR-PLG-4), and may return an `Artifact` to emit it.
    """

    name: ClassVar[str] = ""
    version: ClassVar[str] = "0.1.0"
    api_version: ClassVar[int] = 1
    description: ClassVar[str] = ""
    config_schema: ClassVar[type[BaseModel]] = EmptyConfig
    subscribes: ClassVar[set[Event]] = set()
    #: Declaring this lets the host mark the plugin unavailable — with a reason —
    #: when local-only is on and no local LLM is configured, instead of letting
    #: every invocation fail (FR-CFG-5).
    requires_llm: ClassVar[bool] = False
    #: Never dispatched by an event; runs only when somebody asks for it. For
    #: anything whose cost is a whole-transcript LLM call, spending that on every
    #: session — including the ones nobody will read again — is a decision the
    #: operator should make per recording, and a button they press is how they
    #: make it. `subscribes` still says which handler an on-demand run reaches.
    on_demand: ClassVar[bool] = False
    #: This plugin does something with `ctx.prompt` (FR-PLG-14). Declared rather
    #: than assumed, because it is what the UI offers the prompt picker on: shown
    #: beside a plugin that ignores the choice, it would be a control that lies.
    accepts_prompt: ClassVar[bool] = False

    async def on_session_start(self, ctx: Context) -> Artifact | None:
        return None

    async def on_session_end(self, ctx: Context) -> Artifact | None:
        return None

    async def on_utterance_final(self, ctx: Context) -> Artifact | None:
        return None

    async def on_translation_final(self, ctx: Context) -> Artifact | None:
        return None

    async def on_speaker_changed(self, ctx: Context) -> Artifact | None:
        return None

    async def on_transcript_edited(self, ctx: Context) -> Artifact | None:
        return None

    # --- host interface -----------------------------------------------------

    HANDLERS: ClassVar[dict[Event, str]] = {
        Event.SESSION_START: "on_session_start",
        Event.SESSION_END: "on_session_end",
        Event.UTTERANCE_FINAL: "on_utterance_final",
        Event.TRANSLATION_FINAL: "on_translation_final",
        Event.SPEAKER_CHANGED: "on_speaker_changed",
        Event.TRANSCRIPT_EDITED: "on_transcript_edited",
    }

    def handler_for(self, event: Event) -> Any:
        return getattr(self, self.HANDLERS[event], None)

    def config_json_schema(self) -> dict[str, Any]:
        """The settings form, rendered by the UI from this (FR-PLG-5)."""
        return self.config_schema.model_json_schema()


def format_transcript(
    utterances: Sequence[Utterance],
    speakers: dict[str, Speaker] | None = None,
    *,
    include_speakers: bool = True,
    include_translation: bool = False,
    include_timestamps: bool = True,
) -> str:
    """Render utterances as the plain text a plugin usually wants.

    Shared here rather than in each plugin so that transcript formatting is
    consistent across artifacts, exports, and prompts.
    """
    lines: list[str] = []
    for utt in utterances:
        parts: list[str] = []
        if include_timestamps:
            parts.append(f"[{_clock(utt.start_ms)}]")
        if include_speakers:
            speaker = (speakers or {}).get(utt.speaker_id or "")
            parts.append(f"{speaker.name if speaker else 'Unknown'}:")
        parts.append(utt.text)
        lines.append(" ".join(parts))
        if include_translation and utt.translation:
            lines.append(f"    → {utt.translation}")
    return "\n".join(lines)


def _clock(ms: int) -> str:
    seconds, minutes = (ms // 1000) % 60, ms // 60_000
    if minutes >= 60:
        return f"{minutes // 60:d}:{minutes % 60:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"
