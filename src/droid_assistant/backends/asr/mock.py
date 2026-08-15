"""A deterministic ASR backend for tests, CI, and `--demo`.

It is not a stub: it implements both the batch and the streaming path faithfully
enough to exercise the pipeline, the event stream, and the UI end to end without
a model download. Every integration test in this repository runs against it,
which is what keeps the test suite runnable on a laptop with no weights.

Scripted output comes from `MOCK_TRANSCRIPT` or a script supplied at
construction; otherwise it emits a stable token derived from the audio's energy,
so a silent buffer produces nothing and a loud one produces something.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import numpy as np

from ...domain import ASRCapabilities, ASRResult, AudioBuffer, StreamConfig, Word

_WORDS = (
    "we need to finish this by friday",
    "yes agreed lets pick it up tomorrow",
    "the migration is nearly done",
    "can you send me the numbers",
    "that works for me",
)


@dataclass
class MockASRBackend:
    """Implements `ASRBackend` structurally; not a subclass, to prove the
    interface is usable without inheriting from it."""

    script: list[str] = field(default_factory=list)
    language: str = "en"
    streaming: bool = True
    delay_s: float = 0.0
    silence_rms: float = 1e-4
    calls: int = 0

    @property
    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(
            name="mock",
            streaming=self.streaming,
            languages=None,
            word_timestamps=True,
            local=True,
            confidence=True,
        )

    @property
    def name(self) -> str:
        return "mock"

    async def load(self) -> None: ...

    async def close(self) -> None: ...

    def _text_for(self, audio: AudioBuffer) -> str:
        if self.script:
            return self.script[self.calls % len(self.script)]
        digest = hashlib.sha256(audio.samples.tobytes()[:4096]).digest()
        return _WORDS[digest[0] % len(_WORDS)]

    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if audio.samples.size == 0:
            return []
        if float(np.sqrt(np.mean(audio.samples**2))) < self.silence_rms:
            return []  # silence in, nothing out — the FR-ASR-9 contract
        text = self._text_for(audio)
        self.calls += 1
        return [
            ASRResult(
                text=text,
                start_ms=audio.start_ms,
                end_ms=audio.end_ms,
                language=config.pinned_language or self.language,
                confidence=0.95,
                words=_spread_words(text, audio.start_ms, audio.end_ms),
                no_speech_prob=0.01,
            )
        ]

    async def start_stream(self, config: StreamConfig) -> MockASRStream:
        return MockASRStream(self, config)


def _spread_words(text: str, start_ms: int, end_ms: int) -> list[Word]:
    tokens = text.split()
    if not tokens:
        return []
    step = max(1, (end_ms - start_ms) // len(tokens))
    return [
        Word(w=token, start_ms=start_ms + i * step, end_ms=min(end_ms, start_ms + (i + 1) * step))
        for i, token in enumerate(tokens)
    ]


class MockASRStream:
    """Emits growing prefixes, then the full text — the shape a real streaming
    backend produces, so LocalAgreement and the UI's rewrite handling are both
    genuinely exercised."""

    def __init__(self, backend: MockASRBackend, config: StreamConfig) -> None:
        self._backend = backend
        self._config = config
        self._queue: asyncio.Queue[ASRResult | None] = asyncio.Queue()
        self._closed = False

    async def push(self, audio: AudioBuffer) -> None:
        if self._closed:
            return
        for result in await self._backend.transcribe(audio, self._config):
            tokens = result.text.split()
            for cut in range(1, len(tokens) + 1):
                await self._queue.put(
                    ASRResult(
                        text=" ".join(tokens[:cut]),
                        start_ms=result.start_ms,
                        end_ms=result.end_ms,
                        language=result.language,
                        confidence=result.confidence if cut == len(tokens) else None,
                        words=result.words[:cut],
                    )
                )

    async def results(self) -> AsyncIterator[ASRResult]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def close(self) -> None:
        self._closed = True
        await self._queue.put(None)
