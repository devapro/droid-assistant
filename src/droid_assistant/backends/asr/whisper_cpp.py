"""`whisper.cpp` — the Apple Silicon alternative local backend (SRS §6.2).

CTranslate2 has no Metal path, so on a Mac `faster-whisper` runs on CPU while
this backend reaches the GPU. Which of the two is actually faster on a given Mac
is an empirical question, and answering it is what the eval harness is for.

Its second job is structural: NFR-MNT-1 wants two implementations behind every
interface, and an abstraction with one implementation has not been tested.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ...config import ASRConfig
from ...domain import ASRCapabilities, ASRResult, AudioBuffer, StreamConfig
from .base import ASRBackend, postprocess
from .faster_whisper import WHISPER_LANGUAGES

log = logging.getLogger(__name__)


class WhisperCppBackend(ASRBackend):
    def __init__(self, config: ASRConfig, models_dir: Path) -> None:
        self._config = config
        self._models_dir = models_dir
        self._model: Any = None
        self._lock = asyncio.Semaphore(1)

    @property
    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(
            name=f"whisper.cpp:{self._config.model}",
            streaming=False,
            languages=WHISPER_LANGUAGES,
            word_timestamps=False,  # pywhispercpp exposes segments, not words
            local=True,
            confidence=False,
        )

    async def load(self) -> None:
        if self._model is not None:
            return
        try:
            from pywhispercpp.model import Model
        except ImportError as exc:
            raise RuntimeError("the whisper_cpp backend needs: uv sync --extra whispercpp") from exc
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._model = await asyncio.to_thread(
            Model,
            self._config.model,
            models_dir=str(self._models_dir),
            print_progress=False,
            print_realtime=False,
        )

    async def close(self) -> None:
        self._model = None

    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        if self._model is None:
            await self.load()
        if audio.samples.size == 0:
            return []

        def run() -> list[ASRResult]:
            segments = self._model.transcribe(
                audio.samples,
                language=config.pinned_language or "auto",
                n_threads=0,  # 0 ⇒ the library's own default for this machine
                translate=False,
                initial_prompt=", ".join(config.vocabulary) or None,
            )
            return [
                ASRResult(
                    text=seg.text,
                    # pywhispercpp reports centiseconds.
                    start_ms=audio.start_ms + seg.t0 * 10,
                    end_ms=audio.start_ms + seg.t1 * 10,
                    language=config.pinned_language,
                    words=[],  # no word timings from this library; see capabilities
                )
                for seg in segments
                if seg.text.strip()
            ]

        async with self._lock:
            results = await asyncio.to_thread(run)
        return postprocess(results, config, no_speech_threshold=self._config.no_speech_threshold)
