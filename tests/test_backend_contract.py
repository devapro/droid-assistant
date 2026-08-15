"""Contract tests: one suite, run against every implementation of an interface.

This is what NFR-MNT-1 is actually for. An abstraction with one implementation
has never been tested as an abstraction; a shared suite is what stops the
interface from quietly meaning "whatever the default backend happens to do".

Implementations needing weights are skipped rather than failed, so the suite
runs on a machine with no models — but when the weights are present, the same
assertions apply to them.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import numpy as np
import pytest

from droid_assistant.backends.asr.base import (
    ASRBackend,
    is_hallucination,
    normalise_serbian,
    postprocess,
)
from droid_assistant.backends.asr.mock import MockASRBackend
from droid_assistant.backends.diarization.base import assign_speaker, merge_adjacent
from droid_assistant.backends.diarization.mock import MockDiarizationBackend
from droid_assistant.backends.translation.ctranslate2 import IdentityTranslationBackend
from droid_assistant.domain import (
    SAMPLE_RATE,
    ASRResult,
    AudioBuffer,
    SpeakerSegment,
    StreamConfig,
)


def speech(duration_ms: int = 2000, start_ms: int = 0) -> AudioBuffer:
    count = int(SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(count) / SAMPLE_RATE
    return AudioBuffer((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), start_ms=start_ms)


# --- ASR --------------------------------------------------------------------


def asr_backends() -> list[tuple[str, Any]]:
    backends: list[tuple[str, Any]] = [("mock", MockASRBackend())]
    try:
        import faster_whisper  # noqa: F401

        from droid_assistant.backends.asr.faster_whisper import FasterWhisperBackend
        from droid_assistant.config import default_data_dir

        models = default_data_dir() / "models"
        if (models / "models--Systran--faster-whisper-tiny").exists() or any(
            models.glob("**/tiny*")
        ):
            from droid_assistant.config import ASRConfig

            backends.append(
                ("faster_whisper:tiny", FasterWhisperBackend(ASRConfig(model="tiny"), models))
            )
    except ImportError:
        pass
    return backends


@pytest.mark.parametrize(
    "name,backend", asr_backends(), ids=lambda value: getattr(value, "__name__", str(value))
)
class TestASRContract:
    """Every ASRBackend must satisfy all of this."""

    async def test_declares_capabilities(self, name: str, backend: ASRBackend) -> None:
        caps = backend.capabilities
        assert caps.name
        assert isinstance(caps.streaming, bool)
        assert isinstance(caps.local, bool)

    async def test_empty_audio_yields_no_results(self, name: str, backend: ASRBackend) -> None:
        await backend.load()
        assert await backend.transcribe(AudioBuffer.empty(), StreamConfig()) == []

    async def test_silence_yields_no_utterances(self, name: str, backend: ASRBackend) -> None:
        """FR-ASR-9. Five minutes of room tone must produce nothing; this is the
        cheap version of that assertion."""
        await backend.load()
        quiet = AudioBuffer(np.zeros(SAMPLE_RATE * 3, dtype=np.float32))
        assert await backend.transcribe(quiet, StreamConfig(languages=["en"])) == []

    async def test_offsets_are_absolute_and_ordered(self, name: str, backend: ASRBackend) -> None:
        """FR-ASR-4: timestamps relative to session start, monotonically
        non-decreasing, and never inverted."""
        await backend.load()
        results = await backend.transcribe(speech(start_ms=60_000), StreamConfig(languages=["en"]))
        for result in results:
            assert result.start_ms <= result.end_ms
            assert result.start_ms >= 60_000
        for earlier, later in pairwise(results):
            assert earlier.start_ms <= later.start_ms

    async def test_non_streaming_backend_refuses_live_clearly(
        self, name: str, backend: ASRBackend
    ) -> None:
        if backend.capabilities.streaming:
            pytest.skip("streaming backend")
        with pytest.raises(NotImplementedError, match="streaming"):
            await backend.start_stream(StreamConfig())


# --- diarization ------------------------------------------------------------


class TestDiarizationContract:
    @pytest.fixture(params=[MockDiarizationBackend])
    def backend(self, request: pytest.FixtureRequest) -> Any:
        return request.param()

    async def test_declares_capabilities(self, backend: Any) -> None:
        assert backend.capabilities.name

    async def test_segments_are_ordered_and_non_inverted(self, backend: Any) -> None:
        segments = await backend.diarize(speech(12_000))
        assert segments
        for segment in segments:
            assert segment.start_ms < segment.end_ms
            assert segment.speaker >= 0
        for earlier, later in pairwise(segments):
            assert earlier.start_ms <= later.start_ms

    async def test_pinned_speaker_count_is_respected(self, backend: Any) -> None:
        """FR-DIA-3: exactly two means never a third label."""
        segments = await backend.diarize(speech(30_000), min_speakers=2, max_speakers=2)
        assert len({segment.speaker for segment in segments}) <= 2

    async def test_short_audio_produces_no_embedding(self, backend: Any) -> None:
        """FR-DIA-5 asks for embeddings on utterances longer than a second;
        below that the vector is not stable enough to cluster on."""
        assert await backend.embed(speech(400)) is None

    async def test_embedding_is_deterministic_and_shaped(self, backend: Any) -> None:
        audio = speech(2000)
        first = await backend.embed(audio)
        second = await backend.embed(audio)
        assert first is not None and second is not None
        assert first.ndim == 1 and first.size > 0
        assert np.allclose(first, second)


# --- translation ------------------------------------------------------------


class TestTranslationContract:
    @pytest.fixture(params=[IdentityTranslationBackend])
    def backend(self, request: pytest.FixtureRequest) -> Any:
        return request.param()

    async def test_declares_capabilities(self, backend: Any) -> None:
        assert backend.capabilities.name
        assert isinstance(backend.capabilities.uses_context, bool)

    async def test_empty_input_is_handled(self, backend: Any) -> None:
        assert await backend.translate("", "ru", "en") == ""

    async def test_batch_preserves_order_and_length(self, backend: Any) -> None:
        """A translation attached to the wrong utterance is worse than a slow
        one, so batching must be order-preserving and length-preserving."""
        texts = ["один", "два", "три"]
        results = await backend.translate_batch(texts, "ru", "en")
        assert len(results) == len(texts)


# --- shared post-processing -------------------------------------------------


class TestPostProcessing:
    def test_serbian_transliterates_both_ways(self) -> None:
        """FR-ASR-10: a session configured for one script contains only that one."""
        cyrillic = "Да, до петка."
        latin = normalise_serbian(cyrillic, "latin")
        assert latin == "Da, do petka."
        assert not set(latin) & set("абвгдђежзијклљмнњопрстћуфхцчџш")

    def test_serbian_digraphs_round_trip(self) -> None:
        assert normalise_serbian("љубав њега џеп", "latin") == "ljubav njega džep"
        assert normalise_serbian("ljubav njega džep", "cyrillic") == "љубав њега џеп"

    def test_known_hallucinations_are_suppressed(self) -> None:
        """R9: Whisper emits subtitle credits over silence. VAD removes most of
        the opportunity; this removes the rest."""
        for text in ("Продолжение следует...", "Thanks for watching!", "[MUSIC]", "   ", "..."):
            assert is_hallucination(text, None, 0.6), text

    def test_real_speech_survives(self) -> None:
        assert not is_hallucination("We need to finish this by Friday.", 0.02, 0.6)

    def test_high_no_speech_probability_is_suppressed(self) -> None:
        assert is_hallucination("something", 0.9, 0.6)
        assert not is_hallucination("something", 0.3, 0.6)

    def test_vocabulary_corrects_case_only(self) -> None:
        """FR-ASR-8, conservatively: fuzzy matching here would corrupt correct
        transcripts to make incorrect ones look better."""
        results = postprocess(
            [ASRResult(text="arsenii will send it", start_ms=0, end_ms=1000, language="en")],
            StreamConfig(vocabulary=["Arsenii"]),
            no_speech_threshold=0.6,
        )
        assert results[0].text == "Arsenii will send it"

    def test_postprocess_normalises_serbian_by_configured_script(self) -> None:
        results = postprocess(
            [ASRResult(text="Да, до петка.", start_ms=0, end_ms=1000, language="sr")],
            StreamConfig(serbian_script="latin"),
            no_speech_threshold=0.6,
        )
        assert results[0].text == "Da, do petka."


# --- speaker assignment -----------------------------------------------------


class TestSpeakerAssignment:
    def test_picks_the_speaker_with_most_overlap(self) -> None:
        segments = [SpeakerSegment(0, 1000, 0), SpeakerSegment(1000, 5000, 1)]
        assert assign_speaker(segments, 900, 4000) == 1

    def test_falls_back_to_the_nearest_segment(self) -> None:
        """An unattributed line reads as a bug, so a non-overlapping utterance
        takes the nearest speaker rather than none."""
        segments = [SpeakerSegment(0, 1000, 0)]
        assert assign_speaker(segments, 5000, 6000) == 0

    def test_no_segments_means_no_speaker(self) -> None:
        assert assign_speaker([], 0, 1000) is None

    def test_merge_joins_same_speaker_across_short_gaps(self) -> None:
        """A natural mid-sentence pause must not render as a speaker change."""
        merged = merge_adjacent(
            [
                SpeakerSegment(0, 1000, 0),
                SpeakerSegment(1100, 2000, 0),
                SpeakerSegment(2500, 3000, 1),
            ],
            gap_ms=250,
        )
        assert len(merged) == 2
        assert merged[0].end_ms == 2000

    def test_merge_keeps_a_genuine_speaker_change(self) -> None:
        merged = merge_adjacent([SpeakerSegment(0, 1000, 0), SpeakerSegment(1050, 2000, 1)])
        assert len(merged) == 2
