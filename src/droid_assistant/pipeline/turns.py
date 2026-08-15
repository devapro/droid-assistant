"""How recognised speech becomes messages.

Two mismatches sit between what the segmenter produces and what a reader wants,
and they pull in opposite directions:

* **A segment is not one speaker.** VAD hears speech and silence, not people.
  When two people talk with no gap between them — an interruption, a handover
  mid-sentence — the segmenter emits a single segment covering both, and
  attributing all of it to whoever spoke the most puts the opening words of one
  person's sentence at the end of another person's message. `split_by_speaker`
  cuts such a segment where the diarizer says the speaker changed.
* **A speaker is not one segment.** They pause for breath, and every pause ends
  a segment. `OpenTurn` holds the message open across those pauses, so a
  conversation does not render as a column of one-word lines with the same name
  over each.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..backends.diarization.base import assign_speaker
from ..domain import ASRResult, AudioBuffer, SpeakerSegment, Utterance, Word
from .vad import SpeechSegment


@dataclass(slots=True, frozen=True)
class SpeechRun:
    """One speaker's uninterrupted words within a single recognised segment.

    Usually there is exactly one per segment. More than one means the diarizer
    heard the speaker change while VAD heard continuous speech.
    """

    result: ASRResult
    segment: SpeechSegment
    #: The diarizer's label. Global where whole-session diarization produced it,
    #: and only locally meaningful where a window did — so a caller working from
    #: a window uses it for `continues` and takes identity from an embedding.
    speaker: int | None
    #: Whether this run may extend the message before it. False once the
    #: diarizer has been seen to change speaker inside the segment: that is
    #: direct evidence of a new voice, and it outranks any similarity score.
    continues: bool


def split_by_speaker(
    result: ASRResult, segment: SpeechSegment, turns: Sequence[SpeakerSegment]
) -> list[SpeechRun]:
    """Cut one recognised segment where the speaker changes inside it.

    Word timings are what make the cut possible, so a backend that does not
    report them (`whisper_cpp`, `gigaam`) yields one run for the whole segment,
    attributed by majority exactly as before. The same is true when the diarizer
    saw only one person: the run then carries the recogniser's own text,
    punctuation and spacing intact, rather than a rejoin of its word list.
    """
    speaker = assign_speaker(turns, segment.start_ms, segment.end_ms) if turns else None
    whole = [SpeechRun(result=result, segment=segment, speaker=speaker, continues=True)]
    if not turns or not result.words:
        return whole

    groups = _group_by_speaker(result.words, turns)
    if len(groups) <= 1:
        return whole

    # The runs partition the segment rather than hugging their words, so the
    # silence between two speakers belongs to one of them and the gap arithmetic
    # that joins turns still sees a continuous timeline.
    runs: list[SpeechRun] = []
    for index, (label, words) in enumerate(groups):
        text = " ".join(word.w for word in words).strip()
        if not text:
            continue
        start_ms = segment.start_ms if index == 0 else words[0].start_ms
        end_ms = segment.end_ms if index == len(groups) - 1 else groups[index + 1][1][0].start_ms
        end_ms = max(start_ms, end_ms)
        runs.append(
            SpeechRun(
                result=ASRResult(
                    text=text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    language=result.language,
                    confidence=result.confidence,
                    words=list(words),
                    no_speech_prob=result.no_speech_prob,
                    speaker=result.speaker,
                ),
                segment=SpeechSegment(start_ms, end_ms, truncated=segment.truncated),
                speaker=label,
                continues=index == 0,
            )
        )
    return runs or whole


def _group_by_speaker(
    words: Sequence[Word], turns: Sequence[SpeakerSegment]
) -> list[tuple[int | None, list[Word]]]:
    """Consecutive words sharing a diarizer label, in order."""
    groups: list[tuple[int | None, list[Word]]] = []
    for word in words:
        label = assign_speaker(turns, word.start_ms, word.end_ms)
        if groups and groups[-1][0] == label:
            groups[-1][1].append(word)
        else:
            groups.append((label, [word]))
    return groups


@dataclass(slots=True)
class OpenTurn:
    """One speaker's turn, while it is still being assembled.

    A VAD segment is a breath; a *message* is a turn — everything one person
    says before someone else speaks. Keeping the turn open across their pauses
    is what stops a conversation rendering as a column of one-word lines, and it
    is what gives the recogniser the first half of a sentence as context for the
    second.

    Text is only ever **appended**. Nothing already on screen is rewritten, so
    Balanced keeps the property FR-LAT-5 exists for — no partials, no words
    changing under the reader — while the message itself is allowed to grow.
    """

    utterance: Utterance
    speaker_index: int | None
    #: The VAD endpoints this turn has covered. Deliberately *not* the
    #: utterance's own timestamps: those come back from the recogniser, which
    #: reports where it found words, routinely seconds short of where the audio
    #: ended. Judging a pause by them measures the recogniser's silence trimming
    #: rather than the speaker's pause, and splits a turn that never paused.
    audio_start_ms: int
    audio_end_ms: int
    confidence_sum: float = 0.0
    confidence_ms: int = 0

    @classmethod
    def opened(
        cls,
        utterance: Utterance,
        speaker_index: int | None,
        result: ASRResult,
        audio: AudioBuffer,
        segment: SpeechSegment,
    ) -> OpenTurn:
        turn = cls(
            utterance=utterance,
            speaker_index=speaker_index,
            audio_start_ms=segment.start_ms,
            audio_end_ms=segment.end_ms,
        )
        turn.weigh(result.confidence, audio.duration_ms)
        return turn

    def accepts(
        self, speaker_index: int | None, segment: SpeechSegment, *, gap_ms: int, max_ms: int
    ) -> bool:
        """Does this segment continue the turn, or start a new message?"""
        if gap_ms <= 0:  # joining disabled: one utterance per segment
            return False
        # An unattributed segment joins whatever turn is open. Diarization
        # declines to label the shortest ones — "угу", "да", a laugh, anything
        # under the second an embedder needs — and leaving those as speakerless
        # messages of their own reads as a bug. The caller is responsible for
        # not offering one the diarizer has already said is someone else.
        if speaker_index is not None and speaker_index != self.speaker_index:
            return False
        if segment.start_ms - self.audio_end_ms > gap_ms:
            return False
        return segment.end_ms - self.audio_start_ms <= max_ms

    def extend(self, result: ASRResult, audio: AudioBuffer, segment: SpeechSegment) -> None:
        """Grow the utterance in place. The caller republishes it afterwards."""
        self.audio_end_ms = max(self.audio_end_ms, segment.end_ms)
        utterance = self.utterance
        utterance.text = f"{utterance.text} {result.text.strip()}".strip()
        utterance.end_ms = max(utterance.end_ms, result.end_ms)
        utterance.words.extend(result.words)
        utterance.language = utterance.language or result.language
        self.weigh(result.confidence, audio.duration_ms)

    def weigh(self, confidence: float | None, duration_ms: int) -> None:
        """Confidence for a turn is the mean over its segments weighted by the
        audio each covered, so one two-word aside cannot drag the number for a
        minute of clean speech."""
        if confidence is None or duration_ms <= 0:
            return
        self.confidence_sum += confidence * duration_ms
        self.confidence_ms += duration_ms
        self.utterance.confidence = self.confidence_sum / self.confidence_ms
