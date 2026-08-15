"""The ASR backend interface (SRS §5.6).

Every backend implements `transcribe`. Only a genuinely streaming backend
implements `start_stream`; the rest declare `streaming=False` in their
capabilities and the server refuses Live mode with them at startup instead of
failing mid-session.

`capabilities` is load-bearing: it is what turns "Live mode with a batch-only
backend" from a 40-minute surprise into a startup error.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ...domain import ASRCapabilities, ASRResult, AudioBuffer, StreamConfig


@runtime_checkable
class ASRStream(Protocol):
    """A live recognition stream over a genuinely streaming backend."""

    async def push(self, audio: AudioBuffer) -> None: ...

    # Not `async def`: implementations are async generators, which return their
    # iterator directly rather than a coroutine that yields one.
    def results(self) -> AsyncIterator[ASRResult]: ...

    async def close(self) -> None: ...


class ASRBackend(ABC):
    """Base class rather than a bare Protocol, so the shared normalisation and
    hallucination filtering live in exactly one place."""

    @property
    @abstractmethod
    def capabilities(self) -> ASRCapabilities: ...

    @abstractmethod
    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        """Recognise a complete buffer. Offsets are absolute session time."""

    async def start_stream(self, config: StreamConfig) -> ASRStream:
        raise NotImplementedError(
            f"{self.capabilities.name} is not a streaming backend. Use Balanced or Batch mode, "
            "or configure a streaming backend such as `deepgram` for Live mode."
        )

    async def load(self) -> None:
        """Load weights. Called once at startup so the first utterance of the
        first session is not also the model download (NFR-PORT-4)."""

    async def close(self) -> None: ...

    @property
    def name(self) -> str:
        return self.capabilities.name


# ---------------------------------------------------------------------------
# Shared post-processing
# ---------------------------------------------------------------------------

#: Whisper's training data ends many clips with the same handful of subtitle
#: credits, so it emits them over silence and room tone. VAD gating removes most
#: of the opportunity; this removes the rest (FR-ASR-9, R9).
#: Trailing punctuation is part of the real output — Whisper emits
#: "Продолжение следует..." with the ellipsis — so every pattern tolerates it.
_TRAILING = r"[\s.!…]*$"

HALLUCINATION_PATTERNS = (
    re.compile(
        r"^\s*(?:продолжение следует|субтитры[^\n]*|редактор субтитров[^\n]*)" + _TRAILING, re.I
    ),
    re.compile(r"^\s*(?:thanks? for watching|subscribe|thank you|bye)" + _TRAILING, re.I),
    re.compile(r"^\s*(?:sottotitoli|untertitel|sous-titres|subtítulos)[^\n]*$", re.I),
    re.compile(r"^\s*[\[(][^\])]{0,40}[\])]\s*$"),  # [MUSIC], (silence)
    re.compile(r"^\s*[\W_]+\s*$"),  # punctuation only
)

#: Serbian is written in both scripts and the model picks per window, so a
#: session can come back half-and-half. FR-ASR-10 makes the output consistent.
_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e", "ж": "ž",
    "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n",
    "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "ћ": "ć", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "č", "џ": "dž", "ш": "š",
}  # fmt: skip

_LATIN_DIGRAPHS = (("lj", "љ"), ("nj", "њ"), ("dž", "џ"), ("dz", "џ"))
_LATIN_TO_CYRILLIC = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "đ": "ђ", "e": "е", "ž": "ж",
    "z": "з", "i": "и", "j": "ј", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о",
    "p": "п", "r": "р", "s": "с", "t": "т", "ć": "ћ", "u": "у", "f": "ф", "h": "х",
    "c": "ц", "č": "ч", "š": "ш",
}  # fmt: skip


def _match_case(source: str, converted: str) -> str:
    if source.isupper() and len(source) == 1:
        return converted.upper()
    if source.isupper():
        return converted.upper()
    return converted


def normalise_serbian(text: str, script: str) -> str:
    """Transliterate Serbian to one script. The mapping is bijective at the
    letter level, which is why this is safe in both directions."""
    if script == "latin":
        out: list[str] = []
        for char in text:
            lower = char.lower()
            mapped = _CYRILLIC_TO_LATIN.get(lower)
            out.append(_match_case(char, mapped) if mapped else char)
        return "".join(out)

    text = text.lower()
    for digraph, cyr in _LATIN_DIGRAPHS:
        text = text.replace(digraph, cyr)
    return "".join(_LATIN_TO_CYRILLIC.get(c, c) for c in text)


def is_hallucination(text: str, no_speech_prob: float | None, threshold: float) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if no_speech_prob is not None and no_speech_prob > threshold:
        return True
    return any(pattern.match(stripped) for pattern in HALLUCINATION_PATTERNS)


def apply_vocabulary(text: str, vocabulary: list[str]) -> str:
    """Case-correct known proper nouns the model transcribed phonetically-close.

    Deliberately conservative: only a case-insensitive whole-word match is
    rewritten. Fuzzy matching here would corrupt correct transcripts to make
    incorrect ones look better, and the vocabulary is also passed to the backend
    as a decoding prompt, which is where the real gain comes from (FR-ASR-8).
    """
    for term in vocabulary:
        if not term.strip():
            continue
        text = re.sub(rf"\b{re.escape(term)}\b", term, text, flags=re.IGNORECASE)
    return text


#: Whisper conditions on at most 224 prompt tokens. Cyrillic runs about two
#: tokens per word, so this is a little under that in the worst case and well
#: under it in the best — enough for the last few sentences either way.
PROMPT_CONTEXT_CHARS = 320


def decoding_prompt(config: StreamConfig, *, max_context_chars: int = PROMPT_CONTEXT_CHARS) -> str:
    """The steering text for one recognition call: vocabulary, then context.

    Two things are being asked of the model and they want opposite positions.
    The vocabulary is a standing list of proper nouns (FR-ASR-8). The context is
    the tail of the sentence this call continues, and a prompt is truncated from
    the *front* when it overruns — so the words nearest the audio go last, where
    they survive.
    """
    vocabulary = ", ".join(term for term in config.vocabulary if term.strip())
    context = " ".join(config.context.split())[-max_context_chars:]
    return ". ".join(part for part in (vocabulary, context) if part)


def postprocess(
    results: list[ASRResult], config: StreamConfig, *, no_speech_threshold: float
) -> list[ASRResult]:
    """Filter and normalise raw backend output. Applied by every backend."""
    out: list[ASRResult] = []
    for result in results:
        if is_hallucination(result.text, result.no_speech_prob, no_speech_threshold):
            continue
        text = result.text.strip()
        language = result.language
        if language and language.split("-")[0] in {"sr", "hr", "bs"}:
            text = normalise_serbian(text, config.serbian_script)
        if config.vocabulary:
            text = apply_vocabulary(text, config.vocabulary)
        out.append(
            ASRResult(
                text=text,
                start_ms=result.start_ms,
                end_ms=result.end_ms,
                language=language,
                confidence=result.confidence,
                words=result.words,
                no_speech_prob=result.no_speech_prob,
                speaker=result.speaker,
            )
        )
    return out
