"""sherpa-onnx diarization against real speech, when the models are present.

Two things are checked that a mock cannot: that the native library actually
loads (it is a separate wheel, and a broken one produces an installed package
that cannot be imported), and that the same voice appearing twice in one
recording gets the same label — which is FR-DIA-1's whole content.
"""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from droid_assistant.backends.diarization.sherpa import SherpaDiarizationBackend
from droid_assistant.config import DiarizationConfig, default_data_dir
from droid_assistant.domain import AudioBuffer

MODELS = default_data_dir() / "models"
SEGMENTATION = MODELS / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMBEDDING = MODELS / "nemo_en_titanet_small.onnx"

pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(
        not (SEGMENTATION.exists() and EMBEDDING.exists()),
        reason="diarization models not downloaded (droid-assistant models download)",
    ),
]


def _speak(text: str, voice: str, directory: Path, stem: str) -> np.ndarray:
    say, afconvert = shutil.which("say"), shutil.which("afconvert")
    if not (say and afconvert):
        pytest.skip("no platform text-to-speech available")
    aiff, wav = directory / f"{stem}.aiff", directory / f"{stem}.wav"
    subprocess.run([say, "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(
        [afconvert, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True
    )
    with wave.open(str(wav), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


@pytest.fixture(scope="module")
def two_voices(tmp_path_factory: pytest.TempPathFactory) -> tuple[np.ndarray, np.ndarray]:
    directory = tmp_path_factory.mktemp("voices")
    return (
        _speak("We need to finish the migration by Friday.", "Alex", directory, "a"),
        _speak("Yes, agreed. I will send the numbers tomorrow.", "Daniel", directory, "b"),
    )


@pytest.fixture
def backend() -> SherpaDiarizationBackend:
    return SherpaDiarizationBackend(DiarizationConfig(), MODELS)


async def test_the_native_library_loads(backend: SherpaDiarizationBackend) -> None:
    """The extension and its ONNX Runtime ship as two wheels; when only the
    first is installed, `import sherpa_onnx` fails at `dlopen`."""
    await backend.load()
    assert backend.capabilities.name == "sherpa-onnx"
    await backend.close()


async def test_the_same_voice_gets_the_same_label(
    backend: SherpaDiarizationBackend, two_voices: tuple[np.ndarray, np.ndarray]
) -> None:
    """FR-DIA-1: a label is stable within the session. The first and last turns
    are the same speaker, separated by someone else."""
    first, second = two_voices
    gap = np.zeros(8000, dtype=np.float32)
    audio = AudioBuffer(np.concatenate([first, gap, second, gap, first]))

    await backend.load()
    segments = await backend.diarize(audio)
    await backend.close()

    assert segments
    opening = segments[0]
    closing = segments[-1]
    assert opening.speaker == closing.speaker
    assert opening.start_ms < closing.start_ms


async def test_embeddings_are_produced_and_normalised(
    backend: SherpaDiarizationBackend, two_voices: tuple[np.ndarray, np.ndarray]
) -> None:
    """FR-DIA-5: stored for every utterance from v1, which is the only thing
    that makes retroactive naming possible later."""
    await backend.load()
    embedding = await backend.embed(AudioBuffer(two_voices[0]))
    await backend.close()

    assert embedding is not None
    assert embedding.ndim == 1
    assert embedding.dtype == np.float32
    assert 0.5 < float(np.linalg.norm(embedding)) < 2.0


async def test_different_voices_are_further_apart_than_the_same_voice(
    backend: SherpaDiarizationBackend, two_voices: tuple[np.ndarray, np.ndarray]
) -> None:
    """The property the online clusterer relies on. If this ever stops holding,
    live-mode speaker attribution is unfixable at the clustering layer."""
    from droid_assistant.backends.diarization.base import cosine_similarity

    first, second = two_voices
    await backend.load()
    a1 = await backend.embed(AudioBuffer(first[: len(first) // 2]))
    a2 = await backend.embed(AudioBuffer(first[len(first) // 2 :]))
    b = await backend.embed(AudioBuffer(second))
    await backend.close()

    assert a1 is not None and a2 is not None and b is not None
    same = cosine_similarity(a1, a2)
    different = cosine_similarity(a1, b)
    assert same > different


async def test_a_pinned_count_is_respected(
    backend: SherpaDiarizationBackend, two_voices: tuple[np.ndarray, np.ndarray]
) -> None:
    """FR-DIA-3: setting exactly one speaker never produces a second label."""
    first, second = two_voices
    audio = AudioBuffer(np.concatenate([first, np.zeros(8000, dtype=np.float32), second]))

    await backend.load()
    segments = await backend.diarize(audio, min_speakers=1, max_speakers=1)
    await backend.close()

    assert len({segment.speaker for segment in segments}) == 1


async def test_missing_models_name_the_command(tmp_path: Path) -> None:
    """NFR-REL-6: the model, the path searched, and the command to fetch it."""
    empty = SherpaDiarizationBackend(DiarizationConfig(), tmp_path)
    with pytest.raises(RuntimeError) as raised:
        await empty.load()
    message = str(raised.value)
    assert "models download" in message
    assert str(tmp_path) in message
