"""`faster-whisper` (CTranslate2) — the default local backend (FR-ASR-2).

Whisper is not a streaming model, so this backend is batch-only. Live mode gets
its illusion of streaming from `pipeline/localagreement.py`, which runs this
backend over an overlapping sliding window (SRS §6.2).

Inference is blocking C++, so every call goes to a thread. A single-slot
semaphore serialises them: two concurrent transcriptions on one CTranslate2
model are slower than two sequential ones and use twice the memory.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ...config import ASRConfig
from ...domain import ASRCapabilities, ASRResult, AudioBuffer, StreamConfig, Word
from .base import ASRBackend, postprocess

log = logging.getLogger(__name__)

#: Whisper's language coverage, trimmed to what the config validator needs to
#: check. Serbian is present but its quality is the subject of R1.
WHISPER_LANGUAGES = frozenset(
    [
        "af",
        "am",
        "ar",
        "as",
        "az",
        "ba",
        "be",
        "bg",
        "bn",
        "bo",
        "br",
        "bs",
        "ca",
        "cs",
        "cy",
        "da",
        "de",
        "el",
        "en",
        "es",
        "et",
        "eu",
        "fa",
        "fi",
        "fo",
        "fr",
        "gl",
        "gu",
        "ha",
        "haw",
        "he",
        "hi",
        "hr",
        "ht",
        "hu",
        "hy",
        "id",
        "is",
        "it",
        "ja",
        "jw",
        "ka",
        "kk",
        "km",
        "kn",
        "ko",
        "la",
        "lb",
        "ln",
        "lo",
        "lt",
        "lv",
        "mg",
        "mi",
        "mk",
        "ml",
        "mn",
        "mr",
        "ms",
        "mt",
        "my",
        "ne",
        "nl",
        "nn",
        "no",
        "oc",
        "pa",
        "pl",
        "ps",
        "pt",
        "ro",
        "ru",
        "sa",
        "sd",
        "si",
        "sk",
        "sl",
        "sn",
        "so",
        "sq",
        "sr",
        "su",
        "sv",
        "sw",
        "ta",
        "te",
        "tg",
        "th",
        "tk",
        "tl",
        "tr",
        "tt",
        "uk",
        "ur",
        "uz",
        "vi",
        "yi",
        "yo",
        "zh",
        "yue",
    ]
)


def resolve_device(requested: str) -> tuple[str, str]:
    """`(device, compute_type)` for CTranslate2, chosen from what is present.

    CTranslate2 has no Metal backend, so an Apple Silicon Mac runs on CPU here —
    which is why the SRS keeps `whisper.cpp` as the Apple Silicon alternative.
    """
    if requested in {"auto", "cuda"}:
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda", "float16"
        except Exception:
            pass
        if requested == "cuda":
            log.warning("asr.device = 'cuda' but no CUDA device was found; falling back to CPU")
    return "cpu", "int8"


class FasterWhisperBackend(ASRBackend):
    def __init__(self, config: ASRConfig, models_dir: Path) -> None:
        self._config = config
        self._models_dir = models_dir
        self._model: Any = None
        self._lock = asyncio.Semaphore(1)
        device, compute = resolve_device(config.device)
        self._device = device
        self._compute_type = config.compute_type if config.compute_type != "auto" else compute

    @property
    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(
            name=f"faster-whisper:{self._config.model}",
            streaming=False,
            languages=WHISPER_LANGUAGES,
            word_timestamps=True,
            local=True,
            confidence=True,
        )

    async def load(self) -> None:
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # NFR-REL-6: name the fix, not the traceback
            raise RuntimeError(
                "faster-whisper is not installed. Install the local extra:\n"
                "    uv sync --extra local\n"
                "or configure a cloud backend with `asr.backend = 'deepgram'`."
            ) from exc

        self._models_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "loading ASR model",
            extra={
                "model": self._config.model,
                "device": self._device,
                "compute_type": self._compute_type,
            },
        )
        try:
            self._model = await asyncio.to_thread(
                WhisperModel,
                self._config.model,
                device=self._device,
                compute_type=self._compute_type,
                download_root=str(self._models_dir),
                cpu_threads=max(1, (os.cpu_count() or 4) - 1),
            )
        except Exception as exc:
            raise RuntimeError(
                f"could not load ASR model {self._config.model!r} from {self._models_dir}.\n"
                f"  {exc}\n"
                f"Fetch it with:  droid-assistant models download --asr {self._config.model}"
            ) from exc

    async def close(self) -> None:
        self._model = None

    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        if self._model is None:
            await self.load()
        if audio.samples.size == 0:
            return []

        def run() -> list[ASRResult]:
            segments, _info = self._model.transcribe(
                audio.samples,
                language=config.pinned_language,
                task="transcribe",  # never `translate`: it degrades the transcript (SRS §6.2)
                beam_size=self._config.beam_size,
                word_timestamps=self._config.word_timestamps,
                condition_on_previous_text=self._config.condition_on_previous_text,
                no_speech_threshold=self._config.no_speech_threshold,
                initial_prompt=", ".join(config.vocabulary) or None,  # FR-ASR-8
                vad_filter=False,  # the pipeline has already gated on Silero
            )
            out: list[ASRResult] = []
            for seg in segments:
                words = [
                    Word(
                        w=w.word.strip(),
                        start_ms=audio.start_ms + int(w.start * 1000),
                        end_ms=audio.start_ms + int(w.end * 1000),
                    )
                    for w in (seg.words or [])
                    if w.word.strip()
                ]
                out.append(
                    ASRResult(
                        text=seg.text,
                        start_ms=audio.start_ms + int(seg.start * 1000),
                        end_ms=audio.start_ms + int(seg.end * 1000),
                        language=seg.language if hasattr(seg, "language") else _info.language,
                        # avg_logprob is a log-probability; exp() puts it in [0,1]
                        # for a comparable per-utterance confidence (FR-ASR-11).
                        confidence=_confidence(seg),
                        words=words,
                        no_speech_prob=getattr(seg, "no_speech_prob", None),
                    )
                )
            return out

        async with self._lock:
            results = await asyncio.to_thread(run)
        return postprocess(results, config, no_speech_threshold=self._config.no_speech_threshold)


def _confidence(segment: Any) -> float | None:
    import math

    logprob = getattr(segment, "avg_logprob", None)
    if logprob is None:
        return None
    return round(min(1.0, math.exp(logprob)), 4)
