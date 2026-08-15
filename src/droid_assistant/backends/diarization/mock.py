"""Deterministic diarization for tests and `--demo`.

Alternates speakers on silence boundaries and derives a stable embedding from
the audio, so the clustering, attribution, rename, and palette paths all run
without a model.
"""

from __future__ import annotations

import hashlib

import numpy as np

from ...domain import AudioBuffer, Embedding, SpeakerSegment
from .base import DiarizationCapabilities, merge_adjacent

EMBEDDING_DIM = 192


class MockDiarizationBackend:
    def __init__(self, speakers: int = 2, turn_ms: int = 4000) -> None:
        self.speakers = max(1, speakers)
        self.turn_ms = turn_ms

    @property
    def capabilities(self) -> DiarizationCapabilities:
        return DiarizationCapabilities(name="mock", embeddings=True, local=True)

    @property
    def name(self) -> str:
        return "mock"

    async def load(self) -> None: ...

    async def close(self) -> None: ...

    async def diarize(
        self,
        audio: AudioBuffer,
        *,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        count = self.speakers
        if min_speakers is not None and max_speakers is not None and min_speakers == max_speakers:
            count = min_speakers
        elif max_speakers is not None:
            count = min(count, max_speakers)

        segments: list[SpeakerSegment] = []
        cursor = audio.start_ms
        turn = 0
        while cursor < audio.end_ms:
            end = min(audio.end_ms, cursor + self.turn_ms)
            segments.append(SpeakerSegment(cursor, end, turn % count))
            cursor, turn = end, turn + 1
        return merge_adjacent(segments)

    async def embed(self, audio: AudioBuffer) -> Embedding | None:
        if audio.duration_ms < 1000:
            return None
        seed = int.from_bytes(hashlib.sha256(audio.samples.tobytes()[:8192]).digest()[:4], "big")
        rng = np.random.default_rng(seed)
        vec = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
        return vec / np.linalg.norm(vec)
