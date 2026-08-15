"""`pyannote.audio` 3.1 — the opt-in accuracy backend (SRS §6.2, NFR-MNT-1).

Higher DER than sherpa-onnx, at the cost of PyTorch, a Hugging Face token, and
accepting two gated model licences by hand. That is why it is not the default,
and why it exists: it is the second implementation that keeps the
`DiarizationBackend` abstraction honest.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ...config import DiarizationConfig
from ...domain import SAMPLE_RATE, AudioBuffer, Embedding, SpeakerSegment
from .base import DiarizationBackend, DiarizationCapabilities, merge_adjacent

log = logging.getLogger(__name__)

PIPELINE_ID = "pyannote/speaker-diarization-3.1"
EMBEDDING_ID = "pyannote/embedding"


class PyannoteDiarizationBackend(DiarizationBackend):
    def __init__(self, config: DiarizationConfig, token_env: str = "HF_TOKEN") -> None:
        self._config = config
        self._token_env = token_env
        self._pipeline: Any = None
        self._inference: Any = None
        self._lock = asyncio.Semaphore(1)

    @property
    def capabilities(self) -> DiarizationCapabilities:
        return DiarizationCapabilities(name="pyannote-3.1", embeddings=True, local=True)

    async def load(self) -> None:
        if self._pipeline is not None:
            return
        token = os.environ.get(self._token_env)
        if not token:
            raise RuntimeError(
                f"the pyannote backend needs a Hugging Face token in ${self._token_env}, and "
                f"you must accept the licences for {PIPELINE_ID} and {EMBEDDING_ID} on the Hub "
                "first. The default `sherpa` backend needs neither."
            )
        try:
            import torch
            from pyannote.audio import Inference, Pipeline
        except ImportError as exc:
            raise RuntimeError("the pyannote backend needs: uv sync --extra pyannote") from exc

        def build() -> tuple[Any, Any]:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            pipeline = Pipeline.from_pretrained(PIPELINE_ID, use_auth_token=token).to(device)
            inference = Inference(EMBEDDING_ID, window="whole", use_auth_token=token, device=device)
            return pipeline, inference

        self._pipeline, self._inference = await asyncio.to_thread(build)

    async def close(self) -> None:
        self._pipeline = self._inference = None

    def _as_tensor(self, audio: AudioBuffer) -> dict[str, Any]:
        import torch

        return {
            "waveform": torch.from_numpy(audio.samples).unsqueeze(0),
            "sample_rate": SAMPLE_RATE,
        }

    async def diarize(
        self,
        audio: AudioBuffer,
        *,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        if self._pipeline is None:
            await self.load()
        if audio.duration_ms < 500:
            return []

        lo = min_speakers if min_speakers is not None else self._config.min_speakers
        hi = max_speakers if max_speakers is not None else self._config.max_speakers
        kwargs: dict[str, Any] = {}
        if lo is not None and hi is not None and lo == hi:
            kwargs["num_speakers"] = lo
        else:
            if lo is not None:
                kwargs["min_speakers"] = lo
            if hi is not None:
                kwargs["max_speakers"] = hi

        def run() -> list[SpeakerSegment]:
            annotation = self._pipeline(self._as_tensor(audio), **kwargs)
            # pyannote labels are opaque strings; map them to dense indices in
            # first-appearance order so the palette stays stable (FR-UI-16).
            order: dict[str, int] = {}
            segments: list[SpeakerSegment] = []
            for turn, _, label in annotation.itertracks(yield_label=True):
                index = order.setdefault(label, len(order))
                segments.append(
                    SpeakerSegment(
                        start_ms=audio.start_ms + int(turn.start * 1000),
                        end_ms=audio.start_ms + int(turn.end * 1000),
                        speaker=index,
                    )
                )
            return segments

        async with self._lock:
            segments = await asyncio.to_thread(run)
        return merge_adjacent(segments)

    async def embed(self, audio: AudioBuffer) -> Embedding | None:
        if self._inference is None:
            await self.load()
        if audio.duration_ms < 1000:
            return None

        def run() -> Embedding:
            import numpy as np

            return np.asarray(self._inference(self._as_tensor(audio)), dtype=np.float32).reshape(-1)

        async with self._lock:
            return await asyncio.to_thread(run)
