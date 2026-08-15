"""Voice activity detection (FR-SIG-2).

Silero VAD via ONNX Runtime: about a megabyte, sub-millisecond per frame, and
markedly better than energy thresholding in noise — which matters because the
table condition this system is built for *is* noise.

An energy-based fallback ships alongside it so the pipeline runs with no model
present at all. It is genuinely worse and says so; it exists to keep the system
installable and testable, not to be a real option for recording.

VAD is the single largest cost saving in the pipeline: a 10-minute stream
containing 2 minutes of speech runs ASR over ~3 minutes rather than 10.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ..config import VADConfig
from ..domain import SAMPLE_RATE, Samples

log = logging.getLogger(__name__)

#: Silero expects exactly this many samples per call at 16 kHz.
FRAME_SAMPLES = 512
FRAME_MS = int(FRAME_SAMPLES * 1000 / SAMPLE_RATE)  # 32 ms

#: Silero v5 wants the previous frame's last 64 samples prepended to each frame.
#: Without it the model runs, returns plausible-looking numbers, and reports
#: near-zero speech probability on obvious speech — a silent failure worth a
#: constant and a comment.
CONTEXT_SAMPLES = 64

MODEL_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"


class SpeechProbabilityModel(Protocol):
    def probability(self, frame: Samples) -> float: ...
    def reset(self) -> None: ...


class SileroVAD:
    """ONNX Silero. Stateful: the LSTM state is carried between frames, so
    frames must be fed in order and `reset()` called between sessions."""

    def __init__(self, model_path: Path) -> None:
        self._path = model_path
        self._session: Any = None
        self._state: Any = None
        self._context: Any = None

    def load(self) -> None:
        if self._session is not None:
            return
        try:
            import onnxruntime
        except ImportError as exc:
            raise RuntimeError(
                "vad.backend = 'silero' needs onnxruntime: uv sync --extra local\n"
                "or set vad.backend = 'energy' (noticeably worse in noise)."
            ) from exc
        if not self._path.exists():
            raise RuntimeError(
                f"Silero VAD model not found at {self._path}.\n"
                f"Fetch it with:  droid-assistant models download --vad\n"
                f"Source: {MODEL_URL}"
            )
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1  # one frame at a time; threads only add latency
        options.log_severity_level = 3
        self._session = onnxruntime.InferenceSession(
            str(self._path), providers=["CPUExecutionProvider"], sess_options=options
        )
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)

    def probability(self, frame: Samples) -> float:
        if self._session is None:
            self.load()
        if frame.size != FRAME_SAMPLES:
            padded = np.zeros(FRAME_SAMPLES, dtype=np.float32)
            padded[: min(frame.size, FRAME_SAMPLES)] = frame[:FRAME_SAMPLES]
            frame = padded
        windowed = np.concatenate((self._context, frame)).reshape(1, -1).astype(np.float32)
        out, self._state = self._session.run(
            None,
            {
                "input": windowed,
                "state": self._state,
                "sr": np.array(SAMPLE_RATE, dtype=np.int64),
            },
        )
        self._context = frame[-CONTEXT_SAMPLES:].copy()
        return float(out[0][0])


class EnergyVAD:
    """RMS against an adaptive noise floor. A fallback, not a recommendation.

    It tracks the quietest recent frames as the floor so a constant hum does not
    read as continuous speech — but any non-stationary noise, which is what a
    meeting room actually contains, defeats it.
    """

    def __init__(self, floor_percentile: float = 20.0, history: int = 100) -> None:
        self._history: list[float] = []
        self._percentile = floor_percentile
        self._max_history = history

    def reset(self) -> None:
        self._history.clear()

    def probability(self, frame: Samples) -> float:
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)) + 1e-10)
        self._history.append(rms)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        if len(self._history) < 10:
            return 0.0
        floor = float(np.percentile(self._history, self._percentile)) + 1e-9
        ratio = rms / floor
        # 4× above the floor reads as speech; map 1×–8× onto 0–1.
        return float(np.clip((ratio - 1.0) / 7.0, 0.0, 1.0))


@dataclass(slots=True)
class SpeechSegment:
    """A detected speech region in absolute session time, with padding applied."""

    start_ms: int
    end_ms: int
    truncated: bool = False  # hit `max_speech_ms` rather than a real endpoint

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


class VADSegmenter:
    """Frame-by-frame state machine producing padded speech segments.

    Hysteresis is asymmetric on purpose: speech starts on a single frame over
    threshold (so no word is clipped) but ends only after `min_silence_ms`
    continuously below it (so a pause mid-sentence does not split the utterance).
    """

    def __init__(self, config: VADConfig, model: SpeechProbabilityModel) -> None:
        self._config = config
        self._model = model
        self._pending = np.zeros(0, dtype=np.float32)
        self._position_ms = 0
        self._in_speech = False
        self._speech_start_ms = 0
        self._last_speech_ms = 0
        self._silence_ms = 0

    def reset(self, position_ms: int = 0) -> None:
        self._model.reset()
        self._pending = np.zeros(0, dtype=np.float32)
        self._position_ms = position_ms
        self._in_speech = False
        self._silence_ms = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def feed(self, samples: Samples, start_ms: int | None = None) -> list[SpeechSegment]:
        """Push audio; return any segments that closed within it."""
        if start_ms is not None and not self._in_speech and self._pending.size == 0:
            self._position_ms = start_ms
        self._pending = (
            samples.astype(np.float32)
            if self._pending.size == 0
            else np.concatenate((self._pending, samples.astype(np.float32)))
        )

        segments: list[SpeechSegment] = []
        while self._pending.size >= FRAME_SAMPLES:
            frame = self._pending[:FRAME_SAMPLES]
            self._pending = self._pending[FRAME_SAMPLES:]
            frame_start = self._position_ms
            self._position_ms += FRAME_MS

            is_speech = self._model.probability(frame) >= self._config.threshold

            if is_speech:
                self._silence_ms = 0
                if not self._in_speech:
                    self._in_speech = True
                    self._speech_start_ms = frame_start
                self._last_speech_ms = frame_start + FRAME_MS
                if self._speech_length_ms >= self._config.max_speech_ms:
                    segments.append(self._close(truncated=True))
            elif self._in_speech:
                self._silence_ms += FRAME_MS
                if self._silence_ms >= self._config.min_silence_ms:
                    segment = self._close(truncated=False)
                    if segment.duration_ms >= self._config.min_speech_ms:
                        segments.append(segment)
        return segments

    @property
    def _speech_length_ms(self) -> int:
        return self._last_speech_ms - self._speech_start_ms

    def _close(self, *, truncated: bool) -> SpeechSegment:
        pad = self._config.speech_pad_ms
        segment = SpeechSegment(
            start_ms=max(0, self._speech_start_ms - pad),
            end_ms=self._last_speech_ms + pad,
            truncated=truncated,
        )
        self._in_speech = False
        self._silence_ms = 0
        if truncated:
            # Continue the same utterance from here rather than dropping the
            # audio still being spoken.
            self._in_speech = True
            self._speech_start_ms = self._last_speech_ms
        return segment

    def flush(self) -> SpeechSegment | None:
        """Close an open segment at end of stream, so the last sentence before
        Stop is not lost (the commonest way to lose a whole utterance)."""
        if not self._in_speech:
            return None
        segment = self._close(truncated=False)
        self._in_speech = False
        return segment if segment.duration_ms >= self._config.min_speech_ms else None


def build_vad(config: VADConfig, models_dir: Path) -> SpeechProbabilityModel:
    if config.backend == "energy":
        log.warning(
            "vad.backend = 'energy': noticeably worse in noise than Silero, which is what "
            "meeting rooms contain. Use it only where onnxruntime cannot be installed."
        )
        return EnergyVAD()
    return SileroVAD(models_dir / "silero_vad.onnx")


async def load_vad(model: SpeechProbabilityModel) -> None:
    loader = getattr(model, "load", None)
    if loader is not None:
        await asyncio.to_thread(loader)
