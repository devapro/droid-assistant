"""Turn assembly in Live (FR-LAT-4), Balanced (FR-LAT-5) and Batch (FR-LAT-6).

A VAD segment is a breath; a message is a turn. Two consequences are pinned
here, because both are behaviour a reader notices immediately:

* consecutive segments from one speaker **update** the utterance already
  published rather than adding another one, so a speaker who pauses four times
  produces one message and not five;
* each further segment is recognised with what has already been said in that
  turn as its decoding prompt, which is the context a segment-at-a-time pass
  throws away.

The three modes reach it from different directions. Batch knows the whole
session and walks it; Balanced is mid-recording and cannot see the next segment,
so it has to close a message on a timer instead — and never rewrite what it
already showed. Live is Balanced plus a partial, and the partial is where its
own tests are: which message a final joins decides whether the partial's id is
still the right one to publish under.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from droid_assistant.backends.asr.base import decoding_prompt
from droid_assistant.backends.diarization.base import DiarizationCapabilities
from droid_assistant.backends.translation.ctranslate2 import IdentityTranslationBackend
from droid_assistant.domain import (
    SAMPLE_RATE,
    ASRCapabilities,
    ASRResult,
    AudioBuffer,
    Embedding,
    LatencyMode,
    SpeakerSegment,
    StreamConfig,
    Utterance,
    Word,
    new_id,
    now_ms,
)
from droid_assistant.events import EventBus, EventType
from droid_assistant.pipeline.orchestrator import SessionPipeline
from droid_assistant.pipeline.turns import OpenTurn, split_by_speaker
from droid_assistant.pipeline.vad import SpeechSegment, load_vad
from droid_assistant.store.repository import SessionRecord
from droid_assistant.store.search import SearchIndex

# --- doubles ----------------------------------------------------------------


class ScriptedASR:
    """Reads its answers off a script and remembers the prompt context it was
    handed for each call, which is the half of this feature that is otherwise
    invisible from the outside."""

    def __init__(self, script: list[str], *, undershoot_ms: int = 0) -> None:
        self.script = list(script)
        self.contexts: list[str] = []
        #: How far short of the audio the recogniser claims its last word ended.
        #: Whisper does this routinely; zero is the unrealistic case.
        self.undershoot_ms = undershoot_ms

    @property
    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(
            name="scripted",
            streaming=False,
            languages=None,
            word_timestamps=False,
            local=True,
            confidence=True,
        )

    @property
    def name(self) -> str:
        return "scripted"

    async def load(self) -> None: ...

    async def close(self) -> None: ...

    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        self.contexts.append(config.context)
        text = self.script.pop(0) if self.script else "и так далее"
        end_ms = max(audio.start_ms, audio.end_ms - self.undershoot_ms)
        return [
            ASRResult(
                text=text,
                start_ms=audio.start_ms,
                end_ms=end_ms,
                language="ru",
                confidence=0.9,
                words=spread(text, audio.start_ms, end_ms),
            )
        ]


class ScriptedDiarization:
    """Whole-session turns and per-segment voices, dictated by the test.

    A `voices` entry is consumed per `embed` call: an integer becomes that
    voice's basis vector — orthogonal to every other, so the clusterer sees
    genuinely different people — and None becomes no vector at all, which is
    what a real embedder returns for a segment too short to judge.
    """

    def __init__(
        self, turns: list[SpeakerSegment] | None = None, voices: list[int | None] | None = None
    ) -> None:
        self.turns = turns or []
        self.voices = list(voices) if voices is not None else None

    @property
    def capabilities(self) -> DiarizationCapabilities:
        return DiarizationCapabilities(name="scripted", embeddings=True, local=True)

    @property
    def name(self) -> str:
        return "scripted"

    async def load(self) -> None: ...

    async def close(self) -> None: ...

    async def diarize(self, audio, *, min_speakers=None, max_speakers=None):
        return list(self.turns)

    async def embed(self, audio: AudioBuffer) -> Embedding | None:
        if self.voices is None:
            return np.ones(8, dtype=np.float32) / np.sqrt(8)
        voice = self.voices.pop(0) if self.voices else 0
        if voice is None:
            return None
        vector = np.zeros(8, dtype=np.float32)
        vector[voice] = 1.0
        return vector


def spread(text: str, start_ms: int, end_ms: int) -> list[Word]:
    """Word timings laid out evenly, which is all the splitting needs."""
    tokens = text.split()
    if not tokens or end_ms <= start_ms:
        return []
    step = max(1, (end_ms - start_ms) // len(tokens))
    return [
        Word(w=token, start_ms=start_ms + i * step, end_ms=min(end_ms, start_ms + (i + 1) * step))
        for i, token in enumerate(tokens)
    ]


# --- audio ------------------------------------------------------------------


def tone(duration_ms: int) -> np.ndarray:
    count = int(SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(count) / SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * 220.0 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype(
        np.float32
    )


def silence(duration_ms: int) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * duration_ms / 1000), dtype=np.float32)


def four_bursts() -> np.ndarray:
    """Four utterances of one second, each separated by enough silence for the
    energy VAD to close a segment but not enough to end a turn. The leading
    silence is what gives the energy VAD a noise floor to measure against.

    Segments come out at 376–1800, 2200–3624, 3992–5416 and 5784–7208 ms.
    """
    return np.concatenate(
        [
            silence(600),
            tone(1000),
            silence(800),
            tone(1000),
            silence(800),
            tone(1000),
            silence(800),
            tone(1000),
            silence(600),
        ]
    )


#: Between the third segment and the fourth, so three bursts belong to one
#: speaker and the last to another.
SPEAKER_CHANGES_AT_MS = 5_600


async def build(
    settings, repo, asr, diarization, mode: LatencyMode, *, translate: bool = False
) -> tuple[SessionPipeline, list]:
    """A started-enough pipeline, and a list every published event lands in."""
    record = SessionRecord(
        id=new_id("sess"),
        started_at=now_ms(),
        mode=mode,
        source_languages=["ru"],
        target_language="en" if translate else "ru",
    )
    await repo.create_session(record)
    bus = EventBus()
    events: list = []
    original_publish = bus.publish

    async def spy(session_id, event_type, data):
        events.append((event_type, data))
        await original_publish(session_id, event_type, data)

    bus.publish = spy  # type: ignore[method-assign]

    pipeline = SessionPipeline(
        record,
        settings,
        repo,
        bus,
        asr=asr,
        diarization=diarization,  # type: ignore[arg-type]
        translation=IdentityTranslationBackend() if translate else None,
    )
    await load_vad(pipeline._vad_model)
    return pipeline, events


async def run_batch(settings, repo, asr, diarization, samples) -> tuple[SessionPipeline, list]:
    """Drive one Batch session end to end, returning the pipeline and events."""
    pipeline, events = await build(settings, repo, asr, diarization, LatencyMode.BATCH)
    pipeline._batch_audio.append(samples)
    await pipeline._process_batch()
    return pipeline, events


async def run_balanced(
    settings, repo, asr, diarization, segments: list[SpeechSegment], *, translate: bool = False
) -> tuple[SessionPipeline, list]:
    """Drive Balanced's per-segment path over endpoints the test dictates.

    Segments are handed over one at a time, the way VAD delivers them
    mid-recording, so nothing here can see the next one coming.
    """
    pipeline, events = await build(
        settings, repo, asr, diarization, LatencyMode.BALANCED, translate=translate
    )
    span = max(segment.end_ms for segment in segments)
    pipeline._cache = AudioBuffer(tone(span), start_ms=0)
    for segment in segments:
        await pipeline._process_segment(segment)
    return pipeline, events


async def run_live(
    settings, repo, asr, diarization, segments: list[SpeechSegment], *, translate: bool = False
) -> tuple[SessionPipeline, list]:
    """Drive Live's endpoint path over segments the test dictates.

    Each segment is given the partial id `_live_tick` would have minted for it,
    because what becomes of that id once a final joins a message already on
    screen is half of what these tests are about.
    """
    pipeline, events = await build(
        settings, repo, asr, diarization, LatencyMode.LIVE, translate=translate
    )
    span = max(segment.end_ms for segment in segments)
    pipeline._cache = AudioBuffer(tone(span), start_ms=0)
    for segment in segments:
        pipeline._live_partial_id = new_id("utt")
        pipeline._live_segment_start_ms = segment.start_ms
        await pipeline._finalise_live_segment(segment)
    return pipeline, events


def endpoints(*bounds: tuple[int, int]) -> list[SpeechSegment]:
    return [SpeechSegment(start, end) for start, end in bounds]


# --- the join rule ----------------------------------------------------------


def an_open_turn(*, start_ms: int = 0, end_ms: int = 1000, speaker_index: int | None = 1):
    utterance = Utterance(
        id="utt_1",
        session_id="sess_1",
        seq=0,
        start_ms=start_ms,
        # The recogniser found its last word well before the audio ended, which
        # is the ordinary case and not a degenerate one.
        end_ms=start_ms + 100,
        text="ну понятно",
    )
    return OpenTurn(
        utterance=utterance,
        speaker_index=speaker_index,
        audio_start_ms=start_ms,
        audio_end_ms=end_ms,
    )


class TestWhatContinuesATurn:
    LIMITS = {"gap_ms": 5_000, "max_ms": 120_000}

    def test_the_same_speaker_after_a_pause_continues(self) -> None:
        turn = an_open_turn()
        assert turn.accepts(1, SpeechSegment(2_500, 3_500), **self.LIMITS)

    def test_another_speaker_starts_a_new_message(self) -> None:
        turn = an_open_turn()
        assert not turn.accepts(2, SpeechSegment(1_200, 2_000), **self.LIMITS)

    def test_an_unattributed_segment_joins_whatever_is_open(self) -> None:
        """Diarization declines to label the shortest segments — "угу", "да" —
        and leaving those as speakerless messages of their own reads as a bug."""
        turn = an_open_turn()
        assert turn.accepts(None, SpeechSegment(1_200, 1_500), **self.LIMITS)

    def test_a_long_silence_ends_the_turn(self) -> None:
        turn = an_open_turn()
        assert not turn.accepts(1, SpeechSegment(9_000, 10_000), **self.LIMITS)

    def test_a_turn_does_not_grow_without_bound(self) -> None:
        turn = an_open_turn()
        assert not turn.accepts(1, SpeechSegment(1_200, 200_000), **self.LIMITS)

    def test_a_zero_gap_disables_joining(self) -> None:
        turn = an_open_turn()
        assert not turn.accepts(1, SpeechSegment(1_200, 2_000), gap_ms=0, max_ms=120_000)

    def test_the_pause_is_measured_from_the_audio_not_from_the_last_word(self) -> None:
        """A recogniser reports where it found words, which is routinely seconds
        short of where the segment ended. Measuring the pause from there splits
        turns that never paused."""
        turn = an_open_turn(start_ms=0, end_ms=10_000)
        assert turn.utterance.end_ms == 100  # what the recogniser claimed
        assert turn.accepts(1, SpeechSegment(10_400, 18_000), **self.LIMITS)


# --- the batch pass ---------------------------------------------------------


SCRIPT = ["понятно", "ну понятно", "все я", "счет от газпрома"]


def two_speakers() -> ScriptedDiarization:
    return ScriptedDiarization(
        [
            SpeakerSegment(0, SPEAKER_CHANGES_AT_MS, 0),
            SpeakerSegment(SPEAKER_CHANGES_AT_MS, 20_000, 1),
        ]
    )


class TestBatchTurns:
    async def test_one_speakers_segments_become_one_message(self, settings, repo) -> None:
        asr = ScriptedASR(SCRIPT)
        pipeline, _ = await run_batch(settings, repo, asr, two_speakers(), four_bursts())

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == ["понятно ну понятно все я", "счет от газпрома"]
        # Four segments were recognised; two messages came out of them.
        assert pipeline.stats.asr_calls == 4
        assert pipeline.stats.utterances == 2
        # The merged message spans its whole turn, not just its first breath.
        assert stored[0].end_ms > stored[0].start_ms + 3_000
        assert len({u.speaker_id for u in stored}) == 2

    async def test_the_turn_so_far_is_the_next_segments_prompt(self, settings, repo) -> None:
        asr = ScriptedASR(SCRIPT)
        await run_batch(settings, repo, asr, two_speakers(), four_bursts())

        # The first segment of each turn has nothing to go on; every later one
        # is decoded knowing how its own sentence started.
        assert asr.contexts == ["", "понятно", "понятно ну понятно", ""]

    async def test_the_message_is_updated_rather_than_added_to(self, settings, repo) -> None:
        """The client keys on `utterance_id`, so re-publishing one is an update:
        three events for the first turn, one utterance at the end of them."""
        pipeline, events = await run_batch(
            settings, repo, ScriptedASR(SCRIPT), two_speakers(), four_bursts()
        )

        finals = [data for kind, data in events if kind is EventType.UTTERANCE_FINAL]
        assert len(finals) == 4  # one per segment
        assert len({data["utterance_id"] for data in finals}) == 2  # …under two ids
        assert [data["text"] for data in finals[:3]] == [
            "понятно",
            "понятно ну понятно",
            "понятно ну понятно все я",
        ]
        assert len(await repo.list_utterances(pipeline.session.id)) == 2

    async def test_the_whole_message_stays_searchable(self, settings, repo, db) -> None:
        """Growing an utterance goes through SQLite's UPDATE path, which is a
        different FTS trigger from the INSERT one (FR-SES-10)."""
        pipeline, _ = await run_batch(
            settings, repo, ScriptedASR(SCRIPT), two_speakers(), four_bursts()
        )

        hits = await SearchIndex(db).search("все", session_id=pipeline.session.id)
        assert [hit.utterance_id for hit in hits] == [
            (await repo.list_utterances(pipeline.session.id))[0].id
        ]

    async def test_zero_turn_gap_keeps_one_message_per_segment(self, settings, repo) -> None:
        """The escape hatch, for anyone who wants a message per segment back."""
        unjoined = settings.model_copy(
            update={"vad": settings.vad.model_copy(update={"turn_gap_ms": 0})}
        )
        asr = ScriptedASR(SCRIPT)
        diarization = ScriptedDiarization([SpeakerSegment(0, 20_000, 0)])
        pipeline, _ = await run_batch(unjoined, repo, asr, diarization, four_bursts())

        stored = await repo.list_utterances(pipeline.session.id)
        assert len(stored) == 4
        assert asr.contexts == ["", "", "", ""]


# --- Balanced, which cannot see the next segment ----------------------------

#: Four endpoints a second apart, close enough to be one person's pauses.
BREATHS = ((0, 3_000), (3_400, 6_000), (6_400, 9_000), (9_400, 12_000))


class TestBalancedTurns:
    async def test_a_speakers_pauses_do_not_each_start_a_message(self, settings, repo) -> None:
        asr = ScriptedASR(SCRIPT)
        voices = ScriptedDiarization(voices=[0, 0, 0, 1])
        pipeline, _ = await run_balanced(settings, repo, asr, voices, endpoints(*BREATHS))
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == ["понятно ну понятно все я", "счет от газпрома"]
        assert pipeline.stats.utterances == 2
        assert asr.contexts == ["", "понятно", "понятно ну понятно", ""]

    async def test_nothing_already_shown_is_rewritten(self, settings, repo) -> None:
        """FR-LAT-5 exists so words do not change under the reader. A message
        that grows only ever gains text on the end; every published version is
        a prefix of the one after it."""
        pipeline, events = await run_balanced(
            settings,
            repo,
            ScriptedASR(SCRIPT),
            ScriptedDiarization(voices=[0, 0, 0, 1]),
            endpoints(*BREATHS),
        )
        await pipeline._close_turn()

        assert not [kind for kind, _ in events if kind is EventType.UTTERANCE_PARTIAL]
        shown: dict[str, str] = {}
        for kind, data in events:
            if kind is not EventType.UTTERANCE_FINAL:
                continue
            previous = shown.get(data["utterance_id"], "")
            assert data["text"].startswith(previous)
            shown[data["utterance_id"]] = data["text"]

    async def test_continuous_speech_is_one_message(self, settings, repo) -> None:
        """The case that fails if the pause is measured from the recogniser's
        timestamps: someone talking without stopping, cut into segments every
        `soft_max_speech_ms` by the segmenter, with the recogniser reporting its
        last word seconds before each segment's audio ended.
        """
        asr = ScriptedASR(SCRIPT, undershoot_ms=4_000)
        pipeline, _ = await run_balanced(
            settings,
            repo,
            asr,
            ScriptedDiarization(voices=[0, 0, 0, 0]),
            endpoints((0, 9_000), (9_200, 18_000), (18_200, 27_000), (27_200, 36_000)),
        )
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == [" ".join(SCRIPT)]

    async def test_a_new_voice_starts_a_new_message(self, settings, repo) -> None:
        asr = ScriptedASR(SCRIPT)
        voices = ScriptedDiarization(voices=[0, 1, 0, 1])
        pipeline, _ = await run_balanced(settings, repo, asr, voices, endpoints(*BREATHS))
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == SCRIPT
        assert asr.contexts == ["", "", "", ""]

    async def test_a_silence_closes_the_message_without_another_segment(
        self, settings, repo
    ) -> None:
        """The one thing Batch never needs: mid-recording, the only signal that
        a message ended may be that nothing followed it."""
        pipeline, _ = await run_balanced(
            settings,
            repo,
            ScriptedASR(SCRIPT),
            ScriptedDiarization(voices=[0]),
            endpoints((0, 3_000)),
            translate=True,
        )
        assert pipeline._turn is not None

        # Ingest races ahead while recognition is still catching up. This is the
        # normal state of a local model on a busy machine and says nothing about
        # whether anyone stopped talking, so the message must stay open.
        pipeline._ring.write(np.zeros(SAMPLE_RATE * 30, dtype=np.float32))
        await pipeline._expire_turn()
        assert pipeline._turn is not None

        # Now the audio we have actually listened to runs past the gap in
        # silence: nobody said anything more, and the message is over.
        pipeline._cache = AudioBuffer(tone(1_000), start_ms=12_000)
        await pipeline._expire_turn()

        assert pipeline._turn is None

    async def test_a_growing_message_is_translated_as_it_grows(self, settings, repo) -> None:
        """FR-TRA-4: per utterance, not batched to the end.

        Queueing on turn close instead read well — a whole turn is the better
        unit for the agreement FR-TRA-3 wants — but it meant nothing reached the
        reader until the speaker paused for `turn_gap_ms`, or for `max_turn_ms`
        if they never did. In Live that is the entire mode: the transcript said
        "translating…" for two minutes.
        """
        pipeline, _ = await run_balanced(
            settings,
            repo,
            ScriptedASR(SCRIPT),
            ScriptedDiarization(voices=[0, 0, 0, 1]),
            endpoints(*BREATHS),
            translate=True,
        )
        # One request per segment, all four already asked for, with the turn
        # still open — not one request waiting on a pause that has not come.
        assert pipeline._translation_queue.qsize() == 4
        assert pipeline._turn is not None

        # Each request covers only what its own segment added, so the reader is
        # never shown a re-translation of what they have already read.
        queued = [pipeline._translation_queue.get_nowait() for _ in range(4)]
        assert [item.text for item in queued] == SCRIPT
        # …and the first three belong to one message, the last to another.
        assert len({item.utterance.id for item in queued}) == 2

    async def test_closing_a_turn_asks_for_nothing_more(self, settings, repo) -> None:
        """Everything it contained was translated on the way in."""
        pipeline, _ = await run_balanced(
            settings,
            repo,
            ScriptedASR(SCRIPT),
            ScriptedDiarization(voices=[0, 0, 0, 0]),
            endpoints(*BREATHS),
            translate=True,
        )
        before = pipeline._translation_queue.qsize()
        await pipeline._close_turn()
        assert pipeline._translation_queue.qsize() == before


class TestLiveTurns:
    """Live fragmented for the same reason Balanced did, behind a cause that
    looks like an excuse not to fix it: LocalAgreement settles the words *inside*
    a segment and has no opinion about which message the segment belongs to. So
    the endpoint goes through turn assembly like any other, and what stays
    Live-specific is the partial.
    """

    async def test_a_speakers_pauses_do_not_each_start_a_message(self, settings, repo) -> None:
        asr = ScriptedASR(SCRIPT)
        voices = ScriptedDiarization(voices=[0, 0, 0, 1])
        pipeline, _ = await run_live(settings, repo, asr, voices, endpoints(*BREATHS))
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == ["понятно ну понятно все я", "счет от газпрома"]
        assert pipeline.stats.utterances == 2

    async def test_the_turn_so_far_prompts_the_endpoint_decode(self, settings, repo) -> None:
        """The partial ticks are deliberately left unprompted; this is the one
        decode per segment that keeps the text, so it is the one that gets the
        context."""
        asr = ScriptedASR(SCRIPT)
        voices = ScriptedDiarization(voices=[0, 0, 0, 1])
        await run_live(settings, repo, asr, voices, endpoints(*BREATHS))
        assert asr.contexts == ["", "понятно", "понятно ну понятно", ""]

    async def test_a_new_voice_starts_a_new_message(self, settings, repo) -> None:
        asr = ScriptedASR(SCRIPT)
        voices = ScriptedDiarization(voices=[0, 1, 0, 1])
        pipeline, _ = await run_live(settings, repo, asr, voices, endpoints(*BREATHS))
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == SCRIPT

    async def test_the_partial_keeps_its_id_only_where_it_opens_a_message(
        self, settings, repo
    ) -> None:
        """The client holds one partial and drops it on any final, so a final
        that lands in the previous message clears it without needing its id.
        Handing that id over anyway would republish — and so overwrite — the
        message the partial was sitting under.
        """
        pipeline, events = await build(
            settings,
            repo,
            ScriptedASR(SCRIPT),
            ScriptedDiarization(voices=[0, 0, 0, 1]),
            LatencyMode.LIVE,
        )
        pipeline._cache = AudioBuffer(tone(12_000), start_ms=0)
        minted = []
        for segment in endpoints(*BREATHS):
            pipeline._live_partial_id = new_id("utt")
            minted.append(pipeline._live_partial_id)
            await pipeline._finalise_live_segment(segment)
        await pipeline._close_turn()

        used = {data["utterance_id"] for kind, data in events if kind is EventType.UTTERANCE_FINAL}
        # The first and last segments each opened a message and kept their id.
        # The two in the middle joined one, and theirs went unused.
        assert used == {minted[0], minted[3]}

    async def test_a_segment_holding_two_voices_is_still_cut(self, settings, repo) -> None:
        """Parity with Balanced: someone taking over mid-breath gives VAD one
        segment and the diarizer two speakers, and the words are split between
        the messages rather than all landing on whoever spoke longest."""
        asr = ScriptedASR(["слушай давай еще не будем забывать про инфраструктурный"])
        diarization = ScriptedDiarization(
            [SpeakerSegment(0, 1_500, 0), SpeakerSegment(1_500, 3_000, 1)],
            voices=[0, 1],
        )
        pipeline, _ = await run_live(settings, repo, asr, diarization, endpoints((0, 3_000)))
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert len(stored) == 2
        # The cut lands on a word boundary, wherever the word timings put it.
        assert stored[0].text == "слушай давай еще не"
        assert stored[1].text == "будем забывать про инфраструктурный"
        assert stored[0].speaker_id != stored[1].speaker_id


# --- the prompt itself ------------------------------------------------------


class TestDecodingPrompt:
    def test_context_goes_last_where_truncation_spares_it(self) -> None:
        prompt = decoding_prompt(StreamConfig(vocabulary=["Arsenii", "Gazprom"], context="счет от"))
        assert prompt == "Arsenii, Gazprom. счет от"

    def test_vocabulary_alone_is_unchanged(self) -> None:
        assert decoding_prompt(StreamConfig(vocabulary=["Arsenii"])) == "Arsenii"

    def test_nothing_to_say_is_an_empty_prompt(self) -> None:
        assert decoding_prompt(StreamConfig()) == ""

    def test_a_long_turn_is_trimmed_from_the_front(self) -> None:
        prompt = decoding_prompt(StreamConfig(context="а" * 400 + " конец"), max_context_chars=20)
        assert prompt.endswith("конец")
        assert len(prompt) == 20


# --- one segment, more than one speaker -------------------------------------


class TestSplitBySpeaker:
    RESULT = ASRResult(
        text="Раз два три, четыре пять!",
        start_ms=0,
        end_ms=5_000,
        language="ru",
        confidence=0.9,
        words=spread("раз два три четыре пять", 0, 5_000),
    )
    SEGMENT = SpeechSegment(0, 5_000)

    def test_one_voice_keeps_the_recognisers_own_text(self) -> None:
        """Punctuation and spacing survive, because nothing is rejoined."""
        runs = split_by_speaker(self.RESULT, self.SEGMENT, [SpeakerSegment(0, 5_000, 3)])
        assert [run.result.text for run in runs] == ["Раз два три, четыре пять!"]
        assert [run.speaker for run in runs] == [3]
        assert runs[0].continues

    def test_a_handover_mid_segment_is_cut_where_it_happens(self) -> None:
        runs = split_by_speaker(
            self.RESULT,
            self.SEGMENT,
            [SpeakerSegment(0, 3_000, 0), SpeakerSegment(3_000, 5_000, 1)],
        )
        assert [run.result.text for run in runs] == ["раз два три", "четыре пять"]
        assert [run.speaker for run in runs] == [0, 1]
        # The second voice cannot extend the message the first was building.
        assert [run.continues for run in runs] == [True, False]

    def test_the_runs_partition_the_segment(self) -> None:
        """No audio falls between two runs, so the gap arithmetic that joins
        turns still sees a continuous timeline."""
        runs = split_by_speaker(
            self.RESULT,
            self.SEGMENT,
            [SpeakerSegment(0, 3_000, 0), SpeakerSegment(3_000, 5_000, 1)],
        )
        assert runs[0].segment.start_ms == 0
        assert runs[0].segment.end_ms == runs[1].segment.start_ms
        assert runs[-1].segment.end_ms == 5_000

    def test_without_word_timings_there_is_nothing_to_cut_on(self) -> None:
        """`whisper_cpp` and `gigaam` report no words. They fall back to one run
        attributed by majority, which is what the pipeline did before."""
        wordless = replace(self.RESULT, words=[])
        runs = split_by_speaker(
            wordless,
            self.SEGMENT,
            [SpeakerSegment(0, 3_000, 0), SpeakerSegment(3_000, 5_000, 1)],
        )
        assert len(runs) == 1
        assert runs[0].speaker == 0  # the majority of the segment

    def test_without_diarization_there_is_nothing_to_cut_by(self) -> None:
        runs = split_by_speaker(self.RESULT, self.SEGMENT, [])
        assert len(runs) == 1
        assert runs[0].speaker is None


class TestBalancedSpeakerBoundaries:
    async def test_a_handover_mid_segment_does_not_leak(self, settings, repo) -> None:
        """The reported bug: VAD hears one segment because nobody paused, and
        the opening words of the next speaker's sentence end up at the tail of
        the previous speaker's message."""
        asr = ScriptedASR(["раз два три четыре пять шесть семь восемь девять десять"])
        voices = ScriptedDiarization(
            turns=[SpeakerSegment(0, 7_000, 0), SpeakerSegment(7_000, 10_000, 1)],
            voices=[0, 0, 1],  # the segment, then each run
        )
        pipeline, _ = await run_balanced(settings, repo, asr, voices, endpoints((0, 10_000)))
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == [
            "раз два три четыре пять шесть семь",
            "восемь девять десять",
        ]
        assert stored[0].speaker_id != stored[1].speaker_id

    async def test_a_short_interjection_is_not_absorbed(self, settings, repo) -> None:
        """Under a second there is no usable embedding, so identity has to come
        from the window instead — otherwise someone else's "угу" lands inside
        the message of whoever was talking."""
        asr = ScriptedASR(["давай расскажу тебе в общем в чем история", "угу"])
        voices = ScriptedDiarization(
            turns=[SpeakerSegment(0, 8_000, 0), SpeakerSegment(8_100, 8_800, 1)],
            voices=[0, 0, None, None],
        )
        pipeline, _ = await run_balanced(
            settings, repo, asr, voices, endpoints((0, 8_000), (8_200, 8_700))
        )
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == ["давай расскажу тебе в общем в чем история", "угу"]

    async def test_the_same_speakers_short_aside_is_still_absorbed(self, settings, repo) -> None:
        """The other half of it: their own "угу" mid-thought must not split the
        message, which is what the joining exists for."""
        asr = ScriptedASR(["давай расскажу тебе в общем в чем история", "угу"])
        voices = ScriptedDiarization(
            turns=[SpeakerSegment(0, 8_800, 0)],
            voices=[0, 0, None, None],
        )
        pipeline, _ = await run_balanced(
            settings, repo, asr, voices, endpoints((0, 8_000), (8_200, 8_700))
        )
        await pipeline._close_turn()

        stored = await repo.list_utterances(pipeline.session.id)
        assert [u.text for u in stored] == ["давай расскажу тебе в общем в чем история угу"]
