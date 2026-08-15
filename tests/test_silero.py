"""Silero VAD against real speech, when the model is present.

This exists because of a silent failure found while building: without the
64-sample context Silero v5 expects, the model runs, returns plausible-looking
numbers, and reports near-zero speech probability on obvious speech. Nothing
raises; the transcript is simply empty.

A test that only feeds synthetic tones would not have caught it — a tone is not
speech and Silero is right to say so. So this one uses real speech, generated on
the fly where the platform can, and skips where it cannot.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from droid_assistant.config import VADConfig, default_data_dir
from droid_assistant.pipeline.vad import FRAME_SAMPLES, SileroVAD, VADSegmenter

MODEL = default_data_dir() / "models" / "silero_vad.onnx"

pytestmark = pytest.mark.skipif(
    not MODEL.exists(), reason="silero_vad.onnx not downloaded (droid-assistant models download)"
)


@pytest.fixture(scope="module")
def speech(tmp_path_factory: pytest.TempPathFactory) -> np.ndarray:
    """Real speech at 16 kHz mono, via the platform's text-to-speech."""
    say = shutil.which("say")
    afconvert = shutil.which("afconvert")
    if not (say and afconvert):
        pytest.skip("no platform text-to-speech available to generate real speech")

    directory = tmp_path_factory.mktemp("speech")
    aiff, wav = directory / "s.aiff", directory / "s.wav"
    subprocess.run([say, "-o", str(aiff), "We need to finish this by Friday."], check=True)
    subprocess.run(
        [afconvert, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True
    )
    with wave.open(str(wav), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def test_reports_high_probability_on_real_speech(speech: np.ndarray) -> None:
    """The regression: this returned ~0.003 before the context window was fed."""
    model = SileroVAD(MODEL)
    model.load()
    probabilities = [
        model.probability(speech[i : i + FRAME_SAMPLES])
        for i in range(0, len(speech) - FRAME_SAMPLES, FRAME_SAMPLES)
    ]
    assert max(probabilities) > 0.9
    # Most of a spoken sentence is voiced, so a small fraction over threshold
    # would mean the model is only catching transients.
    assert sum(p > 0.5 for p in probabilities) / len(probabilities) > 0.5


def test_reports_low_probability_on_silence() -> None:
    """NFR-ACC-6: VAD gating is what keeps room tone out of the transcript."""
    model = SileroVAD(MODEL)
    model.load()
    quiet = np.zeros(FRAME_SAMPLES * 60, dtype=np.float32)
    probabilities = [
        model.probability(quiet[i : i + FRAME_SAMPLES])
        for i in range(0, len(quiet) - FRAME_SAMPLES, FRAME_SAMPLES)
    ]
    assert max(probabilities) < 0.5


def test_segments_a_two_turn_conversation(speech: np.ndarray) -> None:
    """The end-to-end shape: two utterances separated by a pause come out as
    two segments, not one and not three."""
    gap = np.zeros(16_000, dtype=np.float32)
    stream = np.concatenate([gap[:8000], speech, gap, speech, gap])

    model = SileroVAD(MODEL)
    model.load()
    segmenter = VADSegmenter(VADConfig(), model)

    segments = []
    for i in range(0, len(stream), 3200):  # 200 ms chunks, as ingest delivers them
        segments += segmenter.feed(stream[i : i + 3200], start_ms=0 if i == 0 else None)
    if (tail := segmenter.flush()) is not None:
        segments.append(tail)

    assert len(segments) == 2
    assert segments[0].end_ms < segments[1].start_ms
    for segment in segments:
        assert segment.duration_ms > 1000


def test_state_resets_between_sessions(speech: np.ndarray) -> None:
    """The model is stateful; a stale LSTM state from the previous session would
    make the first seconds of the next one unreliable."""
    model = SileroVAD(MODEL)
    model.load()
    first = [
        model.probability(speech[i : i + FRAME_SAMPLES])
        for i in range(0, FRAME_SAMPLES * 10, FRAME_SAMPLES)
    ]
    model.reset()
    second = [
        model.probability(speech[i : i + FRAME_SAMPLES])
        for i in range(0, FRAME_SAMPLES * 10, FRAME_SAMPLES)
    ]
    assert first == pytest.approx(second, abs=1e-5)


def test_model_absence_names_the_fix(tmp_path: Path) -> None:
    """NFR-REL-6: the error names the model, the path searched, and the command."""
    model = SileroVAD(tmp_path / "missing.onnx")
    with pytest.raises(RuntimeError) as raised:
        model.load()
    message = str(raised.value)
    assert "missing.onnx" in message
    assert "models download" in message
