"""VAD segmentation (FR-SIG-2) and speaker clustering (FR-DIA-1 … FR-DIA-3).

The segmenter's asymmetric hysteresis is the behaviour worth pinning: speech
starts on one frame over threshold so no word is clipped, but ends only after a
sustained silence so a mid-sentence pause does not split the utterance.
"""

from __future__ import annotations

import numpy as np
import pytest

from droid_assistant.config import VADConfig
from droid_assistant.domain import SAMPLE_RATE
from droid_assistant.pipeline.speakers import OnlineSpeakerClusterer, relabel_by_first_appearance
from droid_assistant.pipeline.vad import FRAME_SAMPLES, EnergyVAD, VADSegmenter


class ScriptedVAD:
    """A VAD whose answers are dictated by the test, so the segmenter's state
    machine is tested in isolation from any model's judgement."""

    def __init__(self, pattern: list[bool]) -> None:
        self.pattern = pattern
        self.index = 0

    def reset(self) -> None:
        self.index = 0

    def probability(self, frame: np.ndarray) -> float:
        value = self.pattern[min(self.index, len(self.pattern) - 1)]
        self.index += 1
        return 1.0 if value else 0.0


def frames(count: int) -> np.ndarray:
    return np.zeros(FRAME_SAMPLES * count, dtype=np.float32)


def config(**kwargs) -> VADConfig:
    return VADConfig(
        threshold=0.5,
        min_speech_ms=kwargs.pop("min_speech_ms", 0),
        min_silence_ms=kwargs.pop("min_silence_ms", 320),  # 10 frames
        speech_pad_ms=kwargs.pop("speech_pad_ms", 0),
        max_speech_ms=kwargs.pop("max_speech_ms", 30_000),
        **kwargs,
    )


class TestSegmentation:
    def test_a_speech_region_closes_after_sustained_silence(self) -> None:
        model = ScriptedVAD([True] * 20 + [False] * 15)
        segmenter = VADSegmenter(config(), model)
        segments = segmenter.feed(frames(35))
        assert len(segments) == 1
        assert segments[0].start_ms == 0
        assert segments[0].end_ms == 20 * 32  # 32 ms per frame

    def test_a_short_pause_does_not_split_an_utterance(self) -> None:
        """Otherwise every comma becomes a new line in the transcript."""
        model = ScriptedVAD([True] * 10 + [False] * 5 + [True] * 10 + [False] * 15)
        segmenter = VADSegmenter(config(min_silence_ms=320), model)
        segments = segmenter.feed(frames(40))
        assert len(segments) == 1
        assert segments[0].end_ms == 25 * 32

    def test_speech_starts_on_the_first_frame_over_threshold(self) -> None:
        """Asymmetric on purpose: waiting for confirmation clips the first word."""
        model = ScriptedVAD([False] * 5 + [True] * 15 + [False] * 15)
        segmenter = VADSegmenter(config(), model)
        segments = segmenter.feed(frames(35))
        assert segments[0].start_ms == 5 * 32

    def test_padding_extends_both_edges(self) -> None:
        model = ScriptedVAD([False] * 5 + [True] * 15 + [False] * 15)
        segmenter = VADSegmenter(config(speech_pad_ms=100), model)
        segments = segmenter.feed(frames(35))
        assert segments[0].start_ms == 5 * 32 - 100
        assert segments[0].end_ms == 20 * 32 + 100

    def test_padding_never_produces_a_negative_start(self) -> None:
        model = ScriptedVAD([True] * 15 + [False] * 15)
        segmenter = VADSegmenter(config(speech_pad_ms=500), model)
        assert segmenter.feed(frames(30))[0].start_ms == 0

    def test_a_short_burst_below_the_minimum_is_dropped(self) -> None:
        """FR-ASR-9's first line of defence: a door closing is not an utterance."""
        model = ScriptedVAD([True] * 2 + [False] * 15)
        segmenter = VADSegmenter(config(min_speech_ms=500), model)
        assert segmenter.feed(frames(20)) == []

    def test_a_monologue_is_truncated_at_the_ceiling(self) -> None:
        """One long speech must not become one forty-minute utterance, but the
        audio still being spoken must not be dropped either."""
        model = ScriptedVAD([True] * 100)
        segmenter = VADSegmenter(config(max_speech_ms=1000), model)
        segments = segmenter.feed(frames(100))
        assert segments
        assert all(segment.truncated for segment in segments)
        assert segmenter.in_speech  # the utterance continues from the cut

    def test_flush_closes_an_open_segment(self) -> None:
        """The last sentence before Stop is the commonest thing to lose."""
        model = ScriptedVAD([True] * 20)
        segmenter = VADSegmenter(config(), model)
        assert segmenter.feed(frames(20)) == []
        tail = segmenter.flush()
        assert tail is not None
        assert tail.end_ms == 20 * 32

    def test_flush_with_no_open_segment_returns_nothing(self) -> None:
        segmenter = VADSegmenter(config(), ScriptedVAD([False] * 20))
        segmenter.feed(frames(20))
        assert segmenter.flush() is None

    def test_partial_frames_are_carried_across_calls(self) -> None:
        """Ingest arrives in 200 ms chunks and VAD consumes 32 ms frames; the
        remainder has to survive the boundary or samples are silently lost."""
        model = ScriptedVAD([True] * 30 + [False] * 20)
        segmenter = VADSegmenter(config(), model)
        block = FRAME_SAMPLES + 100  # deliberately not a whole number of frames
        collected = []
        for i in range(0, FRAME_SAMPLES * 50, block):
            collected += segmenter.feed(
                np.zeros(block, dtype=np.float32), start_ms=0 if i == 0 else None
            )
        assert collected

    def test_offsets_track_absolute_session_time(self) -> None:
        model = ScriptedVAD([True] * 15 + [False] * 15)
        segmenter = VADSegmenter(config(), model)
        segments = segmenter.feed(frames(30), start_ms=60_000)
        assert segments[0].start_ms == 60_000


class TestEnergyVAD:
    def test_silence_reads_as_no_speech(self) -> None:
        model = EnergyVAD()
        for _ in range(20):
            model.probability(np.zeros(FRAME_SAMPLES, dtype=np.float32))
        assert model.probability(np.zeros(FRAME_SAMPLES, dtype=np.float32)) == pytest.approx(0.0)

    def test_a_loud_frame_above_a_quiet_floor_reads_as_speech(self) -> None:
        model = EnergyVAD()
        rng = np.random.default_rng(0)
        for _ in range(30):
            model.probability(rng.normal(scale=0.001, size=FRAME_SAMPLES).astype(np.float32))
        loud = rng.normal(scale=0.2, size=FRAME_SAMPLES).astype(np.float32)
        assert model.probability(loud) > 0.5


class TestSpeakerClustering:
    def unit(self, seed: int, dim: int = 8) -> np.ndarray:
        vector = np.random.default_rng(seed).normal(size=dim).astype(np.float32)
        return vector / np.linalg.norm(vector)

    def test_the_same_voice_keeps_one_label(self) -> None:
        """FR-DIA-1: stable within the session."""
        clusterer = OnlineSpeakerClusterer(threshold=0.5)
        voice = self.unit(1)
        assert clusterer.assign(voice) == 0
        assert clusterer.assign(voice * 0.98) == 0
        assert clusterer.speaker_count == 1

    def test_a_different_voice_gets_a_new_label(self) -> None:
        clusterer = OnlineSpeakerClusterer(threshold=0.9)
        assert clusterer.assign(self.unit(1)) == 0
        assert clusterer.assign(self.unit(2)) == 1

    def test_a_pinned_count_never_invents_a_third_speaker(self) -> None:
        """FR-DIA-3: "exactly 2" on a 2-person recording, and the most effective
        correction available for a known group."""
        clusterer = OnlineSpeakerClusterer(threshold=0.99, min_speakers=2, max_speakers=2)
        assigned = {clusterer.assign(self.unit(seed)) for seed in range(10)}
        assert assigned <= {0, 1}
        assert clusterer.speaker_count == 2

    def test_no_embedding_means_no_speaker(self) -> None:
        """Rather than guessing — FR-DIA-10's principle applied to a missing vector."""
        assert OnlineSpeakerClusterer().assign(None) is None

    def test_the_centroid_follows_the_speaker(self) -> None:
        clusterer = OnlineSpeakerClusterer(threshold=0.5)
        clusterer.assign(self.unit(1))
        clusterer.assign(self.unit(1) * 0.9)
        centroid = clusterer.centroid(0)
        assert centroid is not None
        assert np.isclose(np.linalg.norm(centroid), 1.0, atol=1e-5)

    def test_labels_follow_first_appearance(self) -> None:
        """FR-UI-16: Speaker 1 is whoever spoke first, not whichever cluster the
        algorithm happened to create first."""
        assert relabel_by_first_appearance([3, 1, 3, 0]) == {3: 0, 1: 1, 0: 2}


def test_frame_size_matches_what_silero_expects() -> None:
    assert FRAME_SAMPLES == 512
    assert SAMPLE_RATE == 16_000
