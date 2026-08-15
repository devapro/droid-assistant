"""Domain types shared by the pipeline, the backends, the store, and the API.

These are the vocabulary of the system. Backends speak them (SRS §5.6), the
store persists them (SRS §5.8), and the event stream serialises them (SRS §5.4).
Keeping them free of storage and transport concerns is what lets a backend be
tested without a database and the store be tested without a model.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Self

import numpy as np
from numpy.typing import NDArray

# The pipeline speaks one audio format end to end: 16 kHz mono float32 in
# [-1, 1]. Conversion happens once, at the ingest edge (FR-CAP-3).
SAMPLE_RATE = 16_000
Samples = NDArray[np.float32]
Embedding = NDArray[np.float32]


def new_id(prefix: str) -> str:
    """A sortable, prefixed identifier — `sess_01J8XK...` in the SRS examples."""
    return f"{prefix}_{uuid.uuid4().hex[:22]}"


def now_ms() -> int:
    return int(time.time() * 1000)


class LatencyMode(StrEnum):
    """SRS §3.4. The pipeline is one graph parameterised by this value."""

    LIVE = "live"
    BALANCED = "balanced"
    BATCH = "batch"


class SessionState(StrEnum):
    RECORDING = "recording"
    PROCESSING = "processing"  # Batch mode after stop; plugins still running
    ENDED = "ended"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class Word:
    """A word with its own timing, where the backend supports it (FR-ASR-5)."""

    w: str
    start_ms: int
    end_ms: int

    def to_json(self) -> dict[str, Any]:
        return {"w": self.w, "start_ms": self.start_ms, "end_ms": self.end_ms}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Self:
        return cls(w=raw["w"], start_ms=int(raw["start_ms"]), end_ms=int(raw["end_ms"]))


@dataclass(slots=True)
class Utterance:
    """A contiguous speech segment from one speaker, bounded by VAD endpoints.

    `speaker_id` is None until diarization has run over a *completed* segment.
    Partial hypotheses therefore always carry None, which is FR-DIA-10 expressed
    in the type rather than in a comment.
    """

    id: str
    session_id: str
    seq: int
    start_ms: int
    end_ms: int
    text: str
    language: str | None = None
    speaker_id: str | None = None
    translation: str | None = None
    translation_state: TranslationState = "none"
    confidence: float | None = None
    words: list[Word] = field(default_factory=list)
    text_original: str | None = None  # set on first edit; the pre-edit text
    edited_at: int | None = None
    marked: bool = False  # FR-CAP-18
    is_final: bool = True
    embedding: Embedding | None = None
    timings: dict[str, float] = field(default_factory=dict)  # FR-SIG-4

    def __post_init__(self) -> None:
        if self.start_ms > self.end_ms:
            raise ValueError(
                f"utterance {self.id}: start_ms {self.start_ms} > end_ms {self.end_ms}"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def to_event_data(self) -> dict[str, Any]:
        """The `data` block of an `utterance.*` event (SRS §5.4)."""
        return {
            "utterance_id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "speaker_id": self.speaker_id,
            "language": self.language,
            "text": self.text,
            "translation": self.translation,
            "translation_state": self.translation_state,
            "confidence": self.confidence,
            "marked": self.marked,
            "edited": self.edited_at is not None,
            "words": [w.to_json() for w in self.words],
        }


TranslationState = Literal["none", "pending", "done", "failed", "skipped"]
"""FR-UI-14 needs *pending* and *failed* to be different states, not both "empty"."""


@dataclass(slots=True, frozen=True)
class SpeakerSegment:
    """Diarizer output: a time range attributed to an anonymous speaker index."""

    start_ms: int
    end_ms: int
    speaker: int

    def overlap_ms(self, start_ms: int, end_ms: int) -> int:
        return max(0, min(self.end_ms, end_ms) - max(self.start_ms, start_ms))


@dataclass(slots=True)
class Speaker:
    id: str
    session_id: str
    label: str  # "Speaker 1" — stable within the session (FR-DIA-1)
    index: int  # drives the colourblind-safe palette (FR-UI-16)
    display_name: str | None = None  # set by rename (FR-DIA-4)
    person_id: str | None = None  # v2 enrollment
    centroid_embedding: Embedding | None = None

    @property
    def name(self) -> str:
        return self.display_name or self.label


@dataclass(slots=True)
class AudioBuffer:
    """A block of 16 kHz mono float32 audio with a known session offset."""

    samples: Samples
    start_ms: int = 0
    sample_rate: int = SAMPLE_RATE

    def __post_init__(self) -> None:
        if self.samples.dtype != np.float32:
            self.samples = self.samples.astype(np.float32)

    @property
    def duration_ms(self) -> int:
        return int(len(self.samples) * 1000 / self.sample_rate)

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms

    def slice_ms(self, start_ms: int, end_ms: int) -> AudioBuffer:
        """Sub-buffer by absolute session time, clamped to what this buffer holds."""
        rel_start = max(0, start_ms - self.start_ms)
        rel_end = max(rel_start, end_ms - self.start_ms)
        i = int(rel_start * self.sample_rate / 1000)
        j = min(len(self.samples), int(rel_end * self.sample_rate / 1000))
        return AudioBuffer(
            self.samples[i:j], start_ms=self.start_ms + rel_start, sample_rate=self.sample_rate
        )

    def to_int16(self) -> NDArray[np.int16]:
        pcm: NDArray[np.int16] = np.clip(self.samples * 32768.0, -32768, 32767).astype(np.int16)
        return pcm

    @classmethod
    def from_int16(cls, pcm: NDArray[np.int16], start_ms: int = 0) -> Self:
        return cls(pcm.astype(np.float32) / 32768.0, start_ms=start_ms)

    @classmethod
    def empty(cls, start_ms: int = 0) -> Self:
        return cls(np.zeros(0, dtype=np.float32), start_ms=start_ms)


@dataclass(slots=True, frozen=True)
class StreamConfig:
    """Per-session ASR configuration handed to a backend on every call."""

    languages: list[str] = field(default_factory=list)  # empty ⇒ auto-detect
    target_language: str = "en"
    vocabulary: list[str] = field(default_factory=list)  # FR-ASR-8
    mode: LatencyMode = LatencyMode.BALANCED
    serbian_script: Literal["latin", "cyrillic"] = "latin"  # FR-ASR-10
    #: What has already been recognised in the utterance this call continues.
    #: Balanced and Batch fill it when one speaker's turn spans several VAD
    #: segments: a backend that accepts a decoding prompt then transcribes the
    #: rest of the sentence knowing how it started, which is what keeps a name,
    #: a number, or a case ending consistent across a pause. Empty for the first
    #: segment of a turn, and for backends that cannot be prompted.
    context: str = ""

    @property
    def pinned_language(self) -> str | None:
        """The single language to force, or None when the model must detect.

        With several languages pinned we must *not* force one of them: Whisper
        decodes one language per window, so forcing would mistranscribe the
        others outright. This is the honest half of R13 — the UI warns, and here
        we simply fall back to detection.
        """
        return self.languages[0] if len(self.languages) == 1 else None


@dataclass(slots=True, frozen=True)
class ASRCapabilities:
    """What a backend can do, so an impossible configuration fails at startup
    rather than mid-session (SRS §5.6)."""

    name: str
    streaming: bool
    languages: frozenset[str] | None  # None ⇒ open set / unknown
    word_timestamps: bool
    local: bool  # False ⇒ audio leaves the server (C-4, FR-ASR-3)
    confidence: bool = False
    #: Published list price per minute of audio, for *reporting* spend
    #: (FR-CFG-7). Zero for local backends. A wrong number here costs accuracy
    #: in the estimate, never money.
    price_per_minute_usd: float = 0.0

    def supports_language(self, code: str) -> bool:
        return self.languages is None or code.split("-")[0] in self.languages


@dataclass(slots=True, frozen=True)
class ASRResult:
    """One recognised segment, before it becomes an Utterance."""

    text: str
    start_ms: int
    end_ms: int
    language: str | None = None
    confidence: float | None = None
    words: list[Word] = field(default_factory=list)
    no_speech_prob: float | None = None  # drives hallucination suppression (FR-ASR-9)
    #: A speaker index supplied by the recogniser itself, where the model does
    #: diarization as part of transcription. None means "ask the diarization
    #: backend", which is the usual case.
    speaker: int | None = None


@dataclass(slots=True, frozen=True)
class Artifact:
    """Plugin output attached to a session (SRS §5.5)."""

    kind: str
    content: str
    mime: str = "text/markdown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Usage:
    """Token and cost accounting for one LLM call (FR-CFG-7)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.cost_usd + other.cost_usd,
        )
