"""The per-session pipeline: ingest → VAD → ASR → diarize → translate → emit.

Structure, and why it is this shape:

* **One consumer task** drains the ring buffer and drives VAD. Ingest never
  blocks on recognition; if recognition falls behind, the ring buffer drops
  oldest and counts it (FR-SIG-1) rather than applying back-pressure to a
  browser that cannot slow down anyway.
* **Translation runs on its own queue.** It is slower and less reliable than
  recognition, and FR-TRA-4 requires the transcript to arrive without waiting
  for it. Utterances are published the moment their text exists, then updated
  when a translation lands.
* **Plugins are not here at all.** They observe the event stream from outside
  the pipeline, which is how FR-PLG-9 is satisfied structurally rather than by
  discipline.
* **Persist before publish.** An utterance is committed to SQLite before its
  event goes out, so a client that saw a line can always find it again after a
  crash (FR-SES-3).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from ..backends.asr.base import ASRBackend
from ..backends.diarization.base import DiarizationBackend, assign_speaker
from ..backends.llm.base import BudgetExceeded, LocalOnlyViolation
from ..backends.translation.base import TranslationBackend, same_language
from ..config import Settings
from ..domain import (
    SAMPLE_RATE,
    ASRResult,
    AudioBuffer,
    Embedding,
    LatencyMode,
    Samples,
    SessionState,
    Speaker,
    SpeakerSegment,
    StreamConfig,
    Utterance,
    new_id,
)
from ..events import EventBus, EventType
from ..store.audio import SessionAudioWriter
from ..store.db import dumps
from ..store.repository import Repository, SessionRecord
from .localagreement import LocalAgreement, merge_results
from .modes import ModeProfile, profile_for
from .ringbuffer import RingBuffer
from .speakers import OnlineSpeakerClusterer
from .turns import OpenTurn, SpeechRun, split_by_speaker
from .vad import SpeechSegment, VADSegmenter, build_vad, load_vad

log = logging.getLogger(__name__)

#: How much audio the ring buffer holds. Generous: it is the shock absorber
#: between a browser that cannot pause and a model that sometimes stalls.
RING_CAPACITY_MS = 120_000

#: Slack beyond `vad.max_speech_ms` for the rolling cache a closed segment is
#: sliced out of. Covers the padding, one consumer read, and a late flush.
CACHE_SLACK_MS = 10_000


@dataclass
class PipelineStats:
    dropped_samples: int = 0
    utterances: int = 0
    asr_calls: int = 0
    translation_failures: int = 0
    last_latency_ms: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)

    def record_latency(self, value: float) -> None:
        self.last_latency_ms = value
        self.latencies_ms.append(value)
        if len(self.latencies_ms) > 500:
            del self.latencies_ms[:-500]

    def summary(self) -> dict[str, float | int]:
        values = sorted(self.latencies_ms)
        median = values[len(values) // 2] if values else 0.0
        p95 = values[int(len(values) * 0.95)] if values else 0.0
        return {
            "utterances": self.utterances,
            "asr_calls": self.asr_calls,
            "dropped_ms": int(self.dropped_samples * 1000 / SAMPLE_RATE),
            "translation_failures": self.translation_failures,
            "latency_median_ms": round(median, 1),
            "latency_p95_ms": round(p95, 1),
        }


@dataclass(slots=True)
class _PendingTranslation:
    utterance: Utterance
    source_language: str | None
    #: The source text *this* request covers — one segment, not the whole
    #: message. A turn is translated as it grows, so the reader sees translation
    #: keeping pace with the speech rather than one block arriving whenever the
    #: speaker finally pauses. FR-TRA-4 requires exactly that, and waiting for
    #: the turn to close broke it: in Live a monologue showed "translating…" for
    #: up to `vad.max_turn_ms`.
    text: str


@dataclass(slots=True)
class _Attribution:
    """Who a completed segment belongs to, and the vector that says so."""

    speaker: Speaker | None
    embedding: Embedding | None
    diarization_ms: float


class SessionPipeline:
    """Owns everything that happens to one session's audio."""

    def __init__(
        self,
        session: SessionRecord,
        settings: Settings,
        repo: Repository,
        bus: EventBus,
        *,
        asr: ASRBackend,
        diarization: DiarizationBackend | None = None,
        translation: TranslationBackend | None = None,
        translation_fallback: TranslationBackend | None = None,
        audio_writer: SessionAudioWriter | None = None,
        on_cost: Any = None,  # async (component, usd, provider) -> None
    ) -> None:
        self.session = session
        self.settings = settings
        self.repo = repo
        self.bus = bus
        self.asr = asr
        self.diarization = diarization
        self.translation = translation
        self.translation_fallback = translation_fallback
        self.audio = audio_writer
        self._on_cost = on_cost
        #: Audio actually sent to a paid recogniser. In Live mode the sliding
        #: window re-sends overlapping audio, so this exceeds the session
        #: length — which is the honest number to bill against.
        self.billed_audio_ms = 0

        self.profile: ModeProfile = profile_for(session.mode)
        self.stats = PipelineStats()

        self._ring = RingBuffer(RING_CAPACITY_MS)
        self._vad_model = build_vad(settings.vad, settings.models_dir)
        self._segmenter = VADSegmenter(settings.vad, self._vad_model)
        self._clusterer = OnlineSpeakerClusterer(
            threshold=settings.diarization.clustering_threshold,
            min_speakers=settings.diarization.min_speakers,
            max_speakers=settings.diarization.max_speakers,
        )
        self._speakers: dict[int, Speaker] = {}

        self._stream_config = StreamConfig(
            languages=list(session.source_languages),
            target_language=session.target_language,
            vocabulary=list(session.vocabulary),
            mode=session.mode,
            serbian_script=settings.asr.serbian_script,
        )

        self._tasks: list[asyncio.Task[None]] = []
        self._translation_queue: asyncio.Queue[_PendingTranslation | None] = asyncio.Queue()
        self._context: list[str] = []  # rolling source-language context (FR-TRA-3)
        self._agreement = LocalAgreement()
        #: The message currently being added to. None between turns. All three
        #: modes assemble them: what differs is when a segment's text arrives,
        #: not whether a speaker's breaths belong to one message.
        self._turn: OpenTurn | None = None
        self._live_partial_id: str | None = None
        self._live_segment_start_ms = 0
        self._pending_mode: LatencyMode | None = None
        self._paused = False
        self._pause_started_ms = 0
        self._stopped = asyncio.Event()
        self._seq_lock = asyncio.Lock()
        self._next_seq = 0
        self._degraded_translation = False
        self._last_ingest_ms = 0

        # A closed VAD segment has to be sliced out of audio the consumer has
        # already drained from the ring, so the consumer keeps a rolling copy of
        # the recent past. Bounded by the longest segment VAD can produce.
        self._cache = AudioBuffer.empty()
        self._cache_span_ms = (
            settings.vad.max_speech_ms + 2 * settings.vad.speech_pad_ms + CACHE_SLACK_MS
        )
        # Batch mode holds the session in RAM only when nothing is being written
        # to disk; otherwise it re-reads the writer's PCM file on stop, because a
        # four-hour session is ~900 MB of float32.
        self._batch_audio: list[Samples] = []

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        await load_vad(self._vad_model)
        self._next_seq = await self.repo.next_utterance_seq(self.session.id)
        self._tasks.append(asyncio.create_task(self._consume(), name=f"consume:{self.session.id}"))
        self._tasks.append(
            asyncio.create_task(self._translate_worker(), name=f"translate:{self.session.id}")
        )
        await self.bus.publish(
            self.session.id,
            EventType.SESSION_START,
            {
                "mode": str(self.session.mode),
                "source_languages": self.session.source_languages,
                "target_language": self.session.target_language,
                "started_at": self.session.started_at,
            },
        )

    async def stop(self) -> None:
        """Finalise: flush VAD, run Batch processing, drain translation, close audio."""
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._ring.close()

        consume_task = self._tasks[0] if self._tasks else None
        if consume_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(consume_task, timeout=30)

        # Normally the consumer closed the last message on its way out; this
        # covers the path where it died instead, so a translation is not lost
        # with it.
        await self._close_turn()

        if self.profile.mode is LatencyMode.BATCH:
            await self.repo.update_session(self.session.id, state=SessionState.PROCESSING)
            await self._process_batch()

        await self._translation_queue.put(None)
        for task in self._tasks[1:]:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=120)

        audio_path, duration_ms = (None, 0)
        if self.audio is not None:
            audio_path, duration_ms = await self.audio.finalize()

        ended_at = int(time.time() * 1000)
        await self.repo.update_session(
            self.session.id,
            state=SessionState.ENDED,
            ended_at=ended_at,
            audio_path=str(audio_path) if audio_path else None,
            audio_duration_ms=duration_ms or None,
            dropped_chunks=self.stats.dropped_samples,
        )
        await self.repo.revoke_tokens(self.session.id)
        await self.bus.publish(
            self.session.id,
            EventType.SESSION_END,
            {"ended_at": ended_at, "stats": self.stats.summary()},
        )

    async def abort(self, reason: str) -> None:
        self._stopped.set()
        self._ring.close()
        for task in self._tasks:
            task.cancel()
        await self.bus.publish(self.session.id, EventType.CAPTURE_ERROR, {"reason": reason})

    # --- ingest -------------------------------------------------------------

    async def push(self, samples: Samples, t_ms: int | None = None) -> int:
        """Accept audio from the ingest socket. Returns samples dropped, if any."""
        if self._stopped.is_set() or self._paused:
            return 0
        dropped = self._ring.write(samples)
        if dropped:
            self.stats.dropped_samples += dropped
            log.warning(
                "ring buffer overrun: recognition is behind realtime",
                extra={"session": self.session.id, "dropped_samples": dropped},
            )
        if t_ms is not None:
            self._last_ingest_ms = t_ms
        if self.audio is not None:
            await self.audio.append(samples)
        elif self.profile.mode is LatencyMode.BATCH:
            self._batch_audio.append(samples.copy())
        return dropped

    async def pause(self) -> None:
        """FR-CAP-16. The gap is real time, so the timeline keeps its shape."""
        if self._paused:
            return
        self._paused = True
        self._pause_started_ms = int(time.time() * 1000)
        await self._flush_open_segment()
        await self.bus.publish(self.session.id, EventType.SESSION_PAUSED, {})

    async def resume(self) -> None:
        if not self._paused:
            return
        gap_ms = int(time.time() * 1000) - self._pause_started_ms
        self._paused = False
        if self.audio is not None:
            await self.audio.append_silence(gap_ms)
        # Skip the ring forward so utterance offsets stay aligned with the file.
        self._segmenter.reset(position_ms=self._ring.write_position_ms + gap_ms)
        await self.bus.publish(self.session.id, EventType.SESSION_RESUMED, {"gap_ms": gap_ms})

    @property
    def paused(self) -> bool:
        return self._paused

    # --- mode switching -----------------------------------------------------

    async def request_mode(self, mode: LatencyMode) -> None:
        """FR-LAT-3. The switch is applied at the next VAD boundary, not here.

        Switching mid-utterance is exactly how R8 predicted a finalised utterance
        would be lost: one half of a sentence belongs to the old policy and the
        other to the new. Deferring to a boundary makes the switch trivially
        safe, at a cost of at most one utterance's delay.
        """
        if mode is self.profile.mode:
            return
        self._pending_mode = mode

    async def _apply_pending_mode(self) -> None:
        mode = self._pending_mode
        if mode is None:
            return
        self._pending_mode = None
        previous = self.profile.mode
        self.profile = profile_for(mode)
        self.session.mode = mode
        self._stream_config = StreamConfig(
            languages=self._stream_config.languages,
            target_language=self._stream_config.target_language,
            vocabulary=self._stream_config.vocabulary,
            mode=mode,
            serbian_script=self._stream_config.serbian_script,
        )
        self._agreement.reset()
        self._live_partial_id = None
        await self._close_turn()  # the new mode assembles messages differently
        await self.repo.update_session(self.session.id, mode=mode)
        await self.bus.publish(
            self.session.id,
            EventType.SESSION_MODE_CHANGED,
            {
                "from": str(previous),
                "to": str(mode),
                "at_ms": self._ring.write_position_ms,
            },
        )
        log.info(
            "mode switched",
            extra={"session": self.session.id, "from": str(previous), "to": str(mode)},
        )

    # --- the consumer loop --------------------------------------------------

    async def _consume(self) -> None:
        """Drain the ring buffer, run VAD, and dispatch per mode.

        The loop exits only when the ring is *closed and empty*, never merely
        because stop was requested. Recognition runs behind ingest by design, so
        at the moment Stop is pressed the ring still holds the last few seconds —
        and those seconds are the end of the last sentence someone spoke.
        """
        frame_samples = int(SAMPLE_RATE * 0.2)  # 200 ms, matching the ingest chunk
        try:
            while True:
                got = await self._ring.wait(min_samples=frame_samples, timeout=0.5)
                if self._pending_mode is not None and not self._segmenter.in_speech:
                    await self._apply_pending_mode()

                await self._expire_turn()

                if not got:
                    if self._ring.closed:
                        # Feed whatever is left — a sub-frame tail still carries
                        # the silence that closes the final segment.
                        await self._feed(self._ring.read())
                        break
                    await self._live_tick()
                    continue

                await self._feed(self._ring.read())

            # End of stream: close whatever VAD still had open.
            await self._flush_open_segment()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("pipeline consumer failed", extra={"session": self.session.id})
            await self.bus.publish(
                self.session.id,
                EventType.CAPTURE_ERROR,
                {
                    "reason": "the processing pipeline stopped unexpectedly; "
                    "the session was preserved"
                },
            )

    async def _feed(self, buffer: AudioBuffer) -> None:
        """Cache, segment, and dispatch one block of audio."""
        if buffer.samples.size == 0:
            return
        self._extend_cache(buffer)
        segments = self._segmenter.feed(buffer.samples, start_ms=buffer.start_ms)
        if not self.profile.transcribes_live:
            return
        for segment in segments:
            if self.profile.emits_partials:
                await self._finalise_live_segment(segment)
            else:
                await self._process_segment(segment)
        if self.profile.emits_partials:
            await self._live_tick()

    async def _flush_open_segment(self) -> None:
        segment = self._segmenter.flush()
        if segment is not None:
            if self.profile.emits_partials:
                await self._finalise_live_segment(segment)
            elif self.profile.transcribes_live:
                await self._process_segment(segment)
        # Both callers — a pause, and the end of the stream — are the end of
        # whoever was talking, so the message they were building is over too.
        await self._close_turn()

    # --- Balanced / Batch: one segment at a time ----------------------------

    async def _process_segment(self, segment: SpeechSegment) -> None:
        """FR-LAT-5: recognised once, on the VAD endpoint, never rewritten.

        The segment may land in a message that is already on screen. A speaker
        pausing for breath is still the same speaker mid-thought, and giving
        each breath its own line with their name over it is what turns a
        conversation into a column of one-word messages. So the text is appended
        to the open turn and the utterance republished under its own id, which
        the client applies as an update.

        The order here is diarize → embed → recognise → attribute, and the
        splits matter. The prompt handed to the recogniser depends on whether
        this is the same speaker, so that has to be answered before recognition,
        while the answer cannot be *committed* until after — a backend that
        diarizes for itself has seen the audio in more detail than we have. And
        the segment may hold more than one person, so what comes back is cut
        into runs before any of it becomes a message.
        """
        audio = self._audio_for(segment)
        if audio.samples.size == 0:
            return

        turns = await self._diarize_window(segment)
        embedding, embed_ms = await self._embed(audio)
        context = self._context_for(segment, self._clusterer.nearest(embedding))

        began = time.perf_counter()
        results = await self._transcribe(audio, context=context)
        asr_ms = (time.perf_counter() - began) * 1000
        if not results:
            return

        merged = merge_results(results)
        if merged is None or not merged.text.strip():
            return

        runs = self._runs_for(merged, segment, turns)
        await self._emit_runs(
            runs,
            audio,
            # A window separates the voices inside it and nothing more: its
            # labels are not the same numbers the next window will use, so
            # identity has to keep coming from the clusterer.
            global_labels=False,
            embedding=embedding,
            embed_ms=embed_ms,
            timings={"asr_ms": round(asr_ms, 1)},
            latency_from_ms=segment.end_ms,
        )

    # --- turn assembly ------------------------------------------------------

    def _continues_turn(self, speaker_index: int | None, segment: SpeechSegment) -> bool:
        vad = self.settings.vad
        return self._turn is not None and self._turn.accepts(
            speaker_index, segment, gap_ms=vad.turn_gap_ms, max_ms=vad.max_turn_ms
        )

    def _context_for(self, segment: SpeechSegment, speaker_index: int | None) -> str:
        """What the recogniser should be told this segment continues (FR-ASR-8).

        Empty unless the segment looks like more of the message already open:
        prompting with someone else's words steers the decode towards a sentence
        nobody spoke.
        """
        if self._turn is None or not self._continues_turn(speaker_index, segment):
            return ""
        return self._turn.utterance.text

    async def _diarize_window(self, segment: SpeechSegment) -> list[SpeakerSegment]:
        """Who spoke when, over the recent past.

        Balanced cannot diarize the session, because most of it has not happened
        yet, so it diarizes a trailing window of it — `diarization_window_ms`.
        The window is what makes a short interjection attributable at all: an
        embedder needs about a second of audio before its vector means anything,
        which "угу" does not have, while a segmentation model reading half a
        minute around it does not need one.
        """
        # The profile's 0 means "this mode diarizes the whole session instead";
        # the setting's 0 means the operator turned it off. Either disables it.
        window_ms = min(self.profile.diarization_window_ms, self.settings.diarization.window_ms)
        if self.diarization is None or window_ms <= 0:
            return []
        window = self._cache.slice_ms(
            max(self._cache.start_ms, segment.end_ms - window_ms), segment.end_ms
        )
        if window.duration_ms < 1_000:
            return []
        try:
            return await self.diarization.diarize(
                window,
                min_speakers=self.settings.diarization.min_speakers,
                max_speakers=self.settings.diarization.max_speakers,
            )
        except Exception:
            log.exception("windowed diarization failed", extra={"session": self.session.id})
            return []

    def _runs_for(
        self, result: ASRResult, segment: SpeechSegment, turns: Sequence[SpeakerSegment]
    ) -> list[SpeechRun]:
        """Cut a recognised segment into runs, and decide whether the first of
        them may extend the message already open.

        That second question needs both sides compared under *one* labelling,
        because a window's speaker numbers mean nothing outside it. So this asks
        the window who was talking when the open turn last had audio, and
        compares that to who it says is talking now.
        """
        runs = split_by_speaker(result, segment, turns)
        turn = self._turn
        if not runs or turn is None or not turns:
            return runs
        before = assign_speaker(turns, max(0, turn.audio_end_ms - 1_000), turn.audio_end_ms)
        if before is None or runs[0].speaker is None or before == runs[0].speaker:
            return runs
        return [replace(runs[0], continues=False), *runs[1:]]

    async def _emit_runs(
        self,
        runs: list[SpeechRun],
        audio: AudioBuffer,
        *,
        global_labels: bool,
        embedding: Embedding | None = None,
        embed_ms: float = 0.0,
        timings: dict[str, float] | None = None,
        latency_from_ms: int | None = None,
        utterance_id: str | None = None,
    ) -> None:
        """Publish one segment's runs, in order.

        `global_labels` says whether the diarizer's numbers mean anything beyond
        this call. Whole-session diarization identifies a person for the whole
        recording, so its answer is used directly; a window only separates the
        voices inside itself, so there the label says *that* the speaker changed
        and the embedding says who they are.

        `utterance_id` is Live's partial, offered to the first message this call
        opens so the id survives from partial to final. Only the first: the rest
        of the segment belongs to someone else, and reusing the id there would
        overwrite the message the partial was showing.
        """
        alone = len(runs) == 1
        for index, run in enumerate(runs):
            part = audio if alone else audio.slice_ms(run.segment.start_ms, run.segment.end_ms)
            vector: Embedding | None
            if alone and embedding is not None:
                vector, cost = embedding, embed_ms
            else:
                vector, cost = await self._embed(part)

            # A recogniser that diarizes for itself outranks both.
            speaker_index = run.result.speaker
            if speaker_index is None and global_labels:
                speaker_index = run.speaker
            attribution = await self._attribute(vector, cost, speaker_index=speaker_index)
            await self._add_to_turn(
                run,
                part,
                attribution,
                timings=timings,
                latency_from_ms=latency_from_ms,
                utterance_id=utterance_id if index == 0 else None,
            )

    async def _add_to_turn(
        self,
        run: SpeechRun,
        audio: AudioBuffer,
        attribution: _Attribution,
        *,
        timings: dict[str, float] | None = None,
        latency_from_ms: int | None = None,
        utterance_id: str | None = None,
    ) -> None:
        """Grow the open message, or close it and start the next one."""
        speaker_index = attribution.speaker.index if attribution.speaker else None
        turn = self._turn
        if turn is not None and run.continues and self._continues_turn(speaker_index, run.segment):
            turn.extend(run.result, audio, run.segment)
            utterance = turn.utterance
            # FR-DIA-5: a turn that opened on a segment too short to embed still
            # needs a vector, or it cannot be renamed retroactively later.
            if utterance.embedding is None and attribution.embedding is not None:
                utterance.embedding = attribution.embedding
            utterance.timings["diarization_ms"] = round(
                utterance.timings.get("diarization_ms", 0.0) + attribution.diarization_ms, 1
            )
            await self._publish(utterance, latency_from_ms=latency_from_ms)
            await self._queue_translation(utterance, run.result.text)
            return

        # Someone else started, or the silence ran long.
        await self._close_turn()
        utterance = await self._build_utterance(
            run.result, audio, utterance_id=utterance_id, timings=timings, attribution=attribution
        )
        await self._publish(utterance, latency_from_ms=latency_from_ms)
        self._turn = OpenTurn.opened(utterance, speaker_index, run.result, audio, run.segment)
        await self._queue_translation(utterance, run.result.text)

    async def _queue_translation(self, utterance: Utterance, text: str) -> None:
        """Ask for the translation of what this segment just added.

        Per segment, not per turn. Translating only on turn close reads well —
        a whole turn is the better unit for the agreement FR-TRA-3 exists to get
        right — but it means nothing reaches the reader until the speaker pauses
        for `vad.turn_gap_ms`, or for `vad.max_turn_ms` if they never do. That is
        what FR-TRA-4 forbids, and in Live it is the whole mode. The rolling
        context is what recovers most of the agreement anyway: each request is
        given the source-language history before it.
        """
        text = text.strip()
        if not text:
            return
        # Appended in the same order as the requests, because `_flush_translations`
        # slices this list to find the history *preceding* its batch.
        self._context.append(text)
        if len(self._context) > 64:
            del self._context[:-64]
        if utterance.translation_state in {"pending", "done"} and self._needs_translation(
            utterance
        ):
            await self._translation_queue.put(
                _PendingTranslation(
                    utterance=utterance, source_language=utterance.language, text=text
                )
            )

    async def _close_turn(self) -> None:
        """Finish the open message. Idempotent, so it is safe wherever a turn
        can plausibly end — a pause, a mode switch, the end of the stream."""
        turn, self._turn = self._turn, None
        if turn is not None:
            await self._finish(turn.utterance)

    async def _expire_turn(self) -> None:
        """Close a message nobody has added to for `vad.turn_gap_ms` of audio.

        Without this, the last thing said would stay open until the session did,
        and its translation would wait there with it.

        The clock is the *consumer's* position — how far into the recording we
        have listened — not the ring's write position, which is how far the
        browser has uploaded. Those two are the same only when recognition keeps
        up with realtime. When it does not, and falling behind is the normal
        state of a local model on a busy machine, ingest time runs seconds ahead
        and every turn expires the instant it opens: one message per breath,
        exactly what the joining exists to prevent.
        """
        turn = self._turn
        if turn is None or self._segmenter.in_speech:
            # Mid-sentence: leave it to the VAD endpoint, so whether the message
            # continues depends on the real gap between segments rather than on
            # when this happened to run.
            return
        if self._cache.end_ms - turn.audio_end_ms > self.settings.vad.turn_gap_ms:
            await self._close_turn()

    def _extend_cache(self, buffer: AudioBuffer) -> None:
        """Append to the rolling cache and drop what is older than one segment."""
        if buffer.samples.size == 0:
            return
        if self._cache.samples.size == 0:
            self._cache = AudioBuffer(buffer.samples.copy(), start_ms=buffer.start_ms)
        else:
            self._cache = AudioBuffer(
                np.concatenate((self._cache.samples, buffer.samples)),
                start_ms=self._cache.start_ms,
            )
        excess = self._cache.duration_ms - self._cache_span_ms
        if excess > 0:
            self._cache = self._cache.slice_ms(self._cache.start_ms + excess, self._cache.end_ms)

    def _audio_for(self, segment: SpeechSegment) -> AudioBuffer:
        """Slice a closed speech region out of the rolling cache."""
        return self._cache.slice_ms(segment.start_ms, segment.end_ms)

    # --- Live mode ----------------------------------------------------------

    async def _live_tick(self) -> None:
        """Re-decode the current speech window and publish a partial.

        Runs only while VAD says we are inside speech: decoding silence is how
        Whisper produces the hallucinated subtitle credits FR-ASR-9 filters.
        """
        if not self.profile.emits_partials or not self._segmenter.in_speech:
            return
        cache = self._cache
        if cache.samples.size == 0:
            return
        window_start = max(
            self._agreement.committed_end_ms or self._live_segment_start_ms,
            cache.end_ms - self.profile.window_max_ms,
        )
        window = cache.slice_ms(window_start, cache.end_ms)
        if window.duration_ms < 400:
            return

        results = await self._transcribe(window)
        hypothesis = merge_results(results)
        if hypothesis is None:
            return
        _commit, partial = self._agreement.update(hypothesis)
        if not partial:
            return

        if self._live_partial_id is None:
            self._live_partial_id = new_id("utt")
            self._live_segment_start_ms = window_start
        await self.bus.publish(
            self.session.id,
            EventType.UTTERANCE_PARTIAL,
            {
                "utterance_id": self._live_partial_id,
                "start_ms": self._live_segment_start_ms,
                "end_ms": cache.end_ms,
                # FR-DIA-10: a partial never carries a speaker. Diarization needs
                # a completed segment, and a guess that later flips is worse than
                # an honest placeholder.
                "speaker_id": None,
                "language": hypothesis.language,
                "text": partial,
                "translation": None,
                "translation_state": "none",
                "is_final": False,
            },
        )

    async def _finalise_live_segment(self, segment: SpeechSegment) -> None:
        """Commit the Live partial as a real utterance on the VAD endpoint.

        From here the path is Balanced's, and for the same reason: LocalAgreement
        governs the words *inside* a segment, and has nothing to say about which
        message the segment belongs to. Left to itself it gave every breath its
        own line with the same name over it. So the endpoint goes through turn
        assembly like any other — diarize the window, embed, recognise with the
        turn so far as the prompt, then cut into runs and append.

        What Live keeps of its own is the partial. The client holds one partial
        at a time and drops it the moment a final arrives, so a final that lands
        in the *previous* message clears it correctly without carrying its id.
        The id is still offered, for the ordinary case where this segment starts
        a message rather than continuing one.
        """
        audio = self._audio_for(segment)
        # Reset before anything can fail or return early: a partial id left set
        # here would be handed to the next segment's final, and the id is what
        # decides which message on screen gets overwritten.
        self._agreement.reset()
        partial_id, self._live_partial_id = self._live_partial_id, None
        if audio.samples.size == 0:
            return

        turns = await self._diarize_window(segment)
        embedding, embed_ms = await self._embed(audio)
        # Deliberately only the final decode. The partial ticks re-decode an
        # overlapping window several times a second and LocalAgreement commits
        # what two of them agree on; a prompt that grows underneath that changes
        # what "agree" means mid-segment.
        context = self._context_for(segment, self._clusterer.nearest(embedding))

        results = await self._transcribe(audio, context=context)
        merged = merge_results(results)
        if merged is None or not merged.text.strip():
            return

        runs = self._runs_for(merged, segment, turns)
        await self._emit_runs(
            runs,
            audio,
            global_labels=False,
            embedding=embedding,
            embed_ms=embed_ms,
            latency_from_ms=segment.end_ms,
            utterance_id=partial_id,
        )

    # --- Batch mode ---------------------------------------------------------

    async def _batch_samples(self) -> Samples | None:
        """The whole session's audio, from the writer's file where there is one.

        Re-reading from disk keeps a long Batch session off the heap: four hours
        of float32 is ~900 MB resident, against ~460 MB read once as int16 and
        converted. When audio persistence is off there is no file, and the
        in-memory copy accumulated during ingest is the only source.
        """
        if self.audio is not None and self.audio.raw_path.exists():
            from ..store.audio import read_pcm

            await self.audio.flush()
            return await asyncio.to_thread(read_pcm, self.audio.raw_path)
        if self._batch_audio:
            samples = np.concatenate(self._batch_audio)
            self._batch_audio.clear()
            return samples
        return None

    async def _process_batch(self) -> None:
        """FR-LAT-6: nothing until stop, then everything at once.

        Diarization runs over the entire session here, which is the accurate
        path — clustering with the whole recording available beats the
        incremental approximation the live modes must use.

        Segments are assembled into turns exactly as Balanced does it, with one
        advantage: here the speaker is known before recognition rather than
        guessed at from a running centroid, so the decoding prompt is built on
        an answer instead of a prediction.
        """
        samples = await self._batch_samples()
        if samples is None or samples.size == 0:
            return
        whole = AudioBuffer(samples, start_ms=0)
        log.info(
            "batch processing", extra={"session": self.session.id, "duration_ms": whole.duration_ms}
        )

        segmenter = VADSegmenter(self.settings.vad, self._vad_model)
        segmenter.reset(0)
        segments = segmenter.feed(whole.samples, start_ms=0)
        if (tail := segmenter.flush()) is not None:
            segments.append(tail)
        if not segments:
            return

        diarized = []
        if self.diarization is not None:
            try:
                diarized = await self.diarization.diarize(
                    whole,
                    min_speakers=self.settings.diarization.min_speakers,
                    max_speakers=self.settings.diarization.max_speakers,
                )
            except Exception:
                log.exception("batch diarization failed", extra={"session": self.session.id})

        for segment in segments:
            audio = whole.slice_ms(segment.start_ms, segment.end_ms)
            if audio.samples.size == 0:
                continue

            # Here whole-session diarization has already answered who is
            # speaking, which is what lets the prompt be assembled before
            # recognition rather than guessed at.
            predicted = (
                assign_speaker(diarized, segment.start_ms, segment.end_ms) if diarized else None
            )
            context = self._context_for(segment, predicted)

            results = await self._transcribe(audio, context=context)
            merged = merge_results(results)
            if merged is None or not merged.text.strip():
                continue

            # Whole-session labels identify a person for the whole recording, so
            # unlike Balanced's window this can be trusted for identity too.
            runs = self._runs_for(merged, segment, diarized)
            await self._emit_runs(runs, audio, global_labels=True)

        await self._close_turn()

    # --- shared utterance construction --------------------------------------

    async def _transcribe(self, audio: AudioBuffer, *, context: str = "") -> list[ASRResult]:
        """Recognise one buffer. `context` is the text this call continues, and
        is passed to backends that accept a decoding prompt; the rest ignore it."""
        config = replace(self._stream_config, context=context) if context else self._stream_config
        self.stats.asr_calls += 1
        await self._bill_recognition(audio)
        try:
            return await self.asr.transcribe(audio, config)
        except Exception as exc:
            log.exception("ASR failed", extra={"session": self.session.id})
            await self.bus.publish(
                self.session.id,
                EventType.CAPTURE_ERROR,
                {
                    "component": "asr",
                    "reason": f"transcription failed: {exc}",
                    "remedy": "check Settings → Backends; the session is still recording",
                },
            )
            return []

    async def _bill_recognition(self, audio: AudioBuffer) -> None:
        """Attribute the cost of one recognition call (FR-CFG-7).

        Charged on audio *sent*, before the call rather than after: a request
        that fails part-way has usually still been billed, and under-reporting
        spend is the more dangerous direction to be wrong in.
        """
        caps = self.asr.capabilities
        if caps.local or caps.price_per_minute_usd <= 0 or self._on_cost is None:
            return
        self.billed_audio_ms += audio.duration_ms
        amount = (audio.duration_ms / 60_000) * caps.price_per_minute_usd
        with contextlib.suppress(Exception):
            await self._on_cost("asr", amount, caps.name)

    async def _embed(self, audio: AudioBuffer) -> tuple[Embedding | None, float]:
        """The speaker vector for one completed segment, and what it cost.

        Separate from attribution because the vector is needed *before*
        recognition — it is what says whether the same person is still talking,
        and so what the recogniser is given as its prompt — while committing to
        a speaker cannot happen until after.
        """
        if self.diarization is None:
            return None, 0.0
        began = time.perf_counter()
        embedding = None
        try:
            embedding = await self.diarization.embed(audio)
        except Exception:
            log.exception("embedding failed", extra={"session": self.session.id})
        return embedding, (time.perf_counter() - began) * 1000

    async def _attribute(
        self,
        embedding: Embedding | None,
        embed_ms: float = 0.0,
        *,
        speaker_index: int | None = None,
    ) -> _Attribution:
        """Commit a segment to a speaker, and make sure the label exists.

        A caller that already knows the index — from whole-session diarization,
        or from a recogniser that diarizes for itself, both of which have seen
        more of the audio than our per-utterance clustering can — passes it, and
        the clusterer is left alone.
        """
        began = time.perf_counter()
        if speaker_index is None:
            speaker_index = self._clusterer.assign(embedding)
        elapsed = embed_ms + (time.perf_counter() - began) * 1000

        speaker = await self._speaker_for(speaker_index) if speaker_index is not None else None
        return _Attribution(speaker=speaker, embedding=embedding, diarization_ms=round(elapsed, 1))

    async def _build_utterance(
        self,
        result: ASRResult,
        audio: AudioBuffer,
        *,
        utterance_id: str | None = None,
        timings: dict[str, float] | None = None,
        speaker_index: int | None = None,
        attribution: _Attribution | None = None,
    ) -> Utterance:
        if attribution is None:
            embedding, embed_ms = await self._embed(audio)
            attribution = await self._attribute(embedding, embed_ms, speaker_index=speaker_index)

        async with self._seq_lock:
            seq = self._next_seq
            self._next_seq += 1

        all_timings = {**(timings or {}), "diarization_ms": attribution.diarization_ms}
        return Utterance(
            id=utterance_id or new_id("utt"),
            session_id=self.session.id,
            seq=seq,
            start_ms=max(0, result.start_ms),
            end_ms=max(result.start_ms, result.end_ms),
            text=result.text.strip(),
            language=result.language,
            speaker_id=attribution.speaker.id if attribution.speaker else None,
            confidence=result.confidence,
            words=list(result.words),
            embedding=attribution.embedding,
            timings=all_timings,
            is_final=True,
        )

    async def _speaker_for(self, index: int) -> Speaker:
        speaker = self._speakers.get(index)
        if speaker is None:
            speaker = await self.repo.ensure_speaker(self.session.id, index)
            self._speakers[index] = speaker
            await self.bus.publish(
                self.session.id,
                EventType.SPEAKER_CHANGED,
                {
                    "speaker_id": speaker.id,
                    "label": speaker.label,
                    "display_name": speaker.display_name,
                    "index": speaker.index,
                },
            )
        centroid = self._clusterer.centroid(index)
        if centroid is not None:
            await self.repo.set_speaker_centroid(speaker.id, centroid)
        return speaker

    async def _commit(self, utterance: Utterance, *, latency_from_ms: int | None) -> None:
        """Persist, then publish, then queue for translation — in that order."""
        await self._publish(utterance, latency_from_ms=latency_from_ms)
        await self._finish(utterance)

    async def _publish(self, utterance: Utterance, *, latency_from_ms: int | None = None) -> None:
        """Persist, then publish.

        Idempotent on the utterance id: a message that grows is republished
        under the same id, so both the stored row and the client's copy are
        updated in place rather than duplicated.

        `latency_from_ms` is the VAD endpoint this text answers. Publishing is
        the moment text reaches the reader, which is what NFR-PERF-2 measures,
        and a message that grows reaches them once per segment.
        """
        if not self._needs_translation(utterance):
            utterance.translation_state = "skipped"
        elif utterance.translation_state == "none":
            utterance.translation_state = "pending"
        # A message already showing a translation keeps showing it while the
        # next segment's is in flight. Resetting to "pending" here would blank
        # the line and print "translating…" every time the speaker drew breath.
        await self.repo.add_utterance(utterance)

        if latency_from_ms is not None:
            # Latency against the VAD endpoint, which is the closest server-side
            # proxy for "end of spoken word" (NFR-PERF-2). The client measures
            # the rest of the path.
            elapsed = self._ring.write_position_ms - latency_from_ms
            self.stats.record_latency(max(0.0, float(elapsed)))

        await self.bus.publish(
            self.session.id, EventType.UTTERANCE_FINAL, utterance.to_event_data()
        )

    async def _finish(self, utterance: Utterance) -> None:
        """The half of a commit that happens exactly once: when the message has
        stopped growing, it counts as one message.

        Translation is deliberately *not* here. It is queued per segment as the
        turn grows — see `_queue_translation` — because a reader waiting for the
        speaker to pause is a reader watching "translating…" (FR-TRA-4).
        """
        self.stats.utterances += 1

    def _needs_translation(self, utterance: Utterance) -> bool:
        if self.translation is None or not utterance.text.strip():
            return False
        return not same_language(utterance.language, self.session.target_language)

    # --- translation worker -------------------------------------------------

    async def _translate_worker(self) -> None:
        """Batch consecutive short utterances, subject to a latency ceiling.

        FR-TRA-9 exists because a rapid exchange of one-word turns otherwise
        costs one API round trip per word. The ceiling is what keeps batching
        from breaking FR-TRA-4: we wait at most `batch_max_delay_ms` for a
        companion utterance before sending what we have.
        """
        config = self.settings.translation
        pending: list[_PendingTranslation] = []
        try:
            while True:
                timeout = config.batch_max_delay_ms / 1000 if pending else None
                try:
                    item = await asyncio.wait_for(self._translation_queue.get(), timeout=timeout)
                except TimeoutError:
                    await self._flush_translations(pending)
                    pending = []
                    continue

                if item is None:
                    await self._flush_translations(pending)
                    return

                pending.append(item)
                total_chars = sum(len(p.text) for p in pending)
                if (
                    len(pending) >= config.batch_max_utterances
                    or total_chars >= config.batch_max_chars
                ):
                    await self._flush_translations(pending)
                    pending = []
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("translation worker failed", extra={"session": self.session.id})

    async def _flush_translations(self, pending: list[_PendingTranslation]) -> None:
        if not pending:
            return
        backend = self.translation
        if backend is None:
            return

        target = self.session.target_language
        source = pending[0].source_language
        texts = [p.text for p in pending]
        # Context is the source-language history preceding this batch, which is
        # what fixes pronoun and gender agreement across turns (FR-TRA-3).
        context = self._context[: -len(pending)][-self.settings.translation.context_utterances :]

        began = time.perf_counter()
        # `None` in this list means *this* utterance could not be translated, and
        # renders as the failed state rather than as empty (FR-UI-14).
        translations: list[str | None]
        try:
            translations = list(await backend.translate_batch(texts, source, target, context))
        except (BudgetExceeded, LocalOnlyViolation) as exc:
            translations = await self._translate_via_fallback(texts, source, target, context, exc)
        except Exception as exc:
            log.warning(
                "translation failed",
                extra={"session": self.session.id, "error": str(exc)[:200]},
            )
            translations = await self._translate_via_fallback(texts, source, target, context, exc)

        elapsed_ms = (time.perf_counter() - began) * 1000
        for item, translated in zip(pending, translations, strict=False):
            utterance = item.utterance
            if translated is None:
                self.stats.translation_failures += 1
                # A later segment failing must not discard the part of the turn
                # that already translated: half a translation on screen is worth
                # more than the failed state, and the reader can see which half.
                if not utterance.translation:
                    utterance.translation_state = "failed"
                    utterance.translation = None
                else:
                    log.warning(
                        "a segment of an already-translated message failed; keeping what arrived",
                        extra={"session": self.session.id, "utterance": utterance.id},
                    )
            else:
                # Appended, because the message it belongs to is itself built by
                # appending: this request covered one segment of it.
                utterance.translation = (
                    f"{utterance.translation} {translated}".strip()
                    if utterance.translation
                    else translated
                )
                utterance.translation_state = "done"
            utterance.timings["translation_ms"] = round(elapsed_ms, 1)
            await self.repo.update_utterance(
                utterance.id,
                translation=utterance.translation,
                translation_state=utterance.translation_state,
                timings_json=dumps(utterance.timings),
            )
            await self.bus.publish(
                self.session.id,
                EventType.TRANSLATION_FINAL,
                {
                    "utterance_id": utterance.id,
                    "translation": utterance.translation,
                    "translation_state": utterance.translation_state,
                },
            )

    async def _translate_via_fallback(
        self,
        texts: list[str],
        source: str | None,
        target: str,
        context: list[str],
        cause: Exception,
    ) -> list[str | None]:
        """NFR-REL-4 / FR-CFG-7: degrade to local translation rather than failing.

        The switch is announced once and remembered, so a session that trips its
        budget in minute ten does not retry the cloud for every later utterance.
        """
        if self.translation_fallback is None:
            return [None] * len(texts)
        if not self._degraded_translation:
            self._degraded_translation = True
            log.info(
                "falling back to local translation",
                extra={"session": self.session.id, "cause": type(cause).__name__},
            )
            await self.bus.publish(
                self.session.id,
                EventType.STATUS,
                {
                    "translation_backend": self.translation_fallback.capabilities.name,
                    "reason": str(cause)[:200],
                },
            )
            self.translation = self.translation_fallback
        try:
            return list(
                await self.translation_fallback.translate_batch(texts, source, target, context)
            )
        except Exception:
            log.exception("local translation fallback also failed")
            return [None] * len(texts)

    # --- status -------------------------------------------------------------

    def status(self) -> dict[str, object]:
        ring = self._ring.stats()
        return {
            "session_id": self.session.id,
            "mode": str(self.profile.mode),
            "paused": self._paused,
            "buffered_ms": ring.available_ms,
            "buffer_fill": round(ring.fill_ratio, 3),
            "dropped_ms": ring.dropped_ms,
            "speakers": self._clusterer.speaker_count,
            "billed_audio_ms": self.billed_audio_ms,
            **self.stats.summary(),
        }
