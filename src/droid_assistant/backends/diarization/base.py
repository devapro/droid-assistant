"""The diarization backend interface (SRS §5.6).

Two operations, and they are separable on purpose:

* `diarize` answers *who spoke when* over a window of audio;
* `embed` produces a speaker vector for one utterance.

`embed` is called for every utterance from v1 onward and stored (FR-DIA-5),
because retroactive naming in v2 is only possible if the embeddings exist. It
costs one column and one call now, and cannot be added to history later.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from ...domain import AudioBuffer, Embedding, SpeakerSegment


@dataclass(slots=True, frozen=True)
class DiarizationCapabilities:
    name: str
    embeddings: bool
    local: bool
    needs_speaker_count: bool = False


class DiarizationBackend(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> DiarizationCapabilities: ...

    @abstractmethod
    async def diarize(
        self,
        audio: AudioBuffer,
        *,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]: ...

    @abstractmethod
    async def embed(self, audio: AudioBuffer) -> Embedding | None: ...

    async def load(self) -> None: ...

    async def close(self) -> None: ...

    @property
    def name(self) -> str:
        return self.capabilities.name


def assign_speaker(segments: Sequence[SpeakerSegment], start_ms: int, end_ms: int) -> int | None:
    """Pick the speaker whose segments overlap an utterance the most.

    Overlapping speech is the case this handles badly and knowingly: with two
    people talking at once, one of them wins the utterance. That is the accepted
    consequence of single-microphone capture (SRS §4.2 note), and the DER targets
    for the table condition already account for it.
    """
    if not segments:
        return None
    totals: dict[int, int] = {}
    for seg in segments:
        overlap = seg.overlap_ms(start_ms, end_ms)
        if overlap > 0:
            totals[seg.speaker] = totals.get(seg.speaker, 0) + overlap
    if not totals:
        # No overlap at all — attribute to the nearest segment in time rather
        # than leaving a hole, since an unattributed line reads as a bug.
        nearest = min(
            segments,
            key=lambda s: min(abs(s.start_ms - end_ms), abs(s.end_ms - start_ms)),
        )
        return nearest.speaker
    return max(totals.items(), key=lambda kv: kv[1])[0]


def cosine_similarity(a: Embedding, b: Embedding) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return 0.0 if denom == 0 else float(np.dot(a, b) / denom)


def merge_adjacent(segments: list[SpeakerSegment], gap_ms: int = 250) -> list[SpeakerSegment]:
    """Join consecutive same-speaker segments separated by a short gap.

    Without this, a natural mid-sentence pause becomes a speaker change in the
    UI, which reads as a diarization failure even when attribution is correct.
    """
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: s.start_ms)
    merged = [ordered[0]]
    for seg in ordered[1:]:
        last = merged[-1]
        if seg.speaker == last.speaker and seg.start_ms - last.end_ms <= gap_ms:
            merged[-1] = SpeakerSegment(last.start_ms, max(last.end_ms, seg.end_ms), last.speaker)
        else:
            merged.append(seg)
    return merged
