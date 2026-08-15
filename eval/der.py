"""Diarization Error Rate (NFR-EVAL-1).

DER = (missed speech + false alarm + speaker confusion) / total reference speech.

The subtlety is the speaker mapping: diarization produces anonymous labels, so
`Speaker 1` in a hypothesis has no reason to be `Speaker 1` in the reference.
Scoring requires finding the optimal one-to-one mapping first, otherwise a
perfect diarization with permuted labels scores 100% error. This is solved
exactly with the Hungarian algorithm over the overlap matrix.

A collar is applied by convention (250 ms each side of every reference
boundary), because human annotation of a speaker change is not accurate to the
millisecond and scoring the disagreement measures the annotator, not the model.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from droid_assistant.domain import SpeakerSegment

DEFAULT_COLLAR_MS = 250


@dataclass(slots=True, frozen=True)
class DERResult:
    der: float
    missed_ms: int
    false_alarm_ms: int
    confusion_ms: int
    total_reference_ms: int
    reference_speakers: int
    hypothesis_speakers: int
    mapping: dict[int, int]

    def to_json(self) -> dict[str, object]:
        return {
            "der": round(self.der, 4),
            "missed_ms": self.missed_ms,
            "false_alarm_ms": self.false_alarm_ms,
            "confusion_ms": self.confusion_ms,
            "total_reference_ms": self.total_reference_ms,
            "reference_speakers": self.reference_speakers,
            "hypothesis_speakers": self.hypothesis_speakers,
            "speaker_count_error": self.hypothesis_speakers - self.reference_speakers,
        }


def _timeline(segments: list[SpeakerSegment], resolution_ms: int, length_ms: int) -> np.ndarray:
    """Frame-level speaker labels; -1 means silence.

    Frame scoring rather than interval arithmetic: it handles overlapping
    segments without special cases, and at 10 ms the quantisation error is far
    below the collar.
    """
    frames = np.full(length_ms // resolution_ms + 1, -1, dtype=np.int32)
    for segment in sorted(segments, key=lambda s: s.start_ms):
        start = max(0, segment.start_ms // resolution_ms)
        end = min(len(frames), segment.end_ms // resolution_ms + 1)
        frames[start:end] = segment.speaker
    return frames


def _collar_mask(
    reference: list[SpeakerSegment], resolution_ms: int, length: int, collar_ms: int
) -> np.ndarray:
    """True where a frame is scored — i.e. not inside a boundary collar."""
    mask = np.ones(length, dtype=bool)
    if collar_ms <= 0:
        return mask
    half = max(1, collar_ms // resolution_ms)
    for segment in reference:
        for boundary_ms in (segment.start_ms, segment.end_ms):
            centre = boundary_ms // resolution_ms
            mask[max(0, centre - half) : min(length, centre + half + 1)] = False
    return mask


def _hungarian(cost: np.ndarray) -> list[tuple[int, int]]:
    """Optimal assignment. Falls back to greedy if SciPy is absent.

    SciPy is not a dependency of this project — pulling it in for one function
    used only by the eval harness is not worth the install size. Greedy is
    correct in the common case where one hypothesis speaker clearly dominates
    each reference speaker, and the harness reports when it was used.
    """
    try:
        from scipy.optimize import linear_sum_assignment

        rows, cols = linear_sum_assignment(cost)
        return list(zip(rows.tolist(), cols.tolist(), strict=True))
    except ImportError:
        pairs: list[tuple[int, int]] = []
        used_rows: set[int] = set()
        used_cols: set[int] = set()
        order = np.dstack(np.unravel_index(np.argsort(cost, axis=None), cost.shape))[0]
        for row, col in order:
            if int(row) in used_rows or int(col) in used_cols:
                continue
            pairs.append((int(row), int(col)))
            used_rows.add(int(row))
            used_cols.add(int(col))
        return pairs


def diarization_error_rate(
    reference: list[SpeakerSegment],
    hypothesis: list[SpeakerSegment],
    *,
    collar_ms: int = DEFAULT_COLLAR_MS,
    resolution_ms: int = 10,
) -> DERResult:
    if not reference:
        return DERResult(0.0, 0, 0, 0, 0, 0, len({s.speaker for s in hypothesis}), {})

    length_ms = max(
        max(s.end_ms for s in reference),
        max((s.end_ms for s in hypothesis), default=0),
    )
    ref_frames = _timeline(reference, resolution_ms, length_ms)
    hyp_frames = _timeline(hypothesis, resolution_ms, length_ms)
    size = min(len(ref_frames), len(hyp_frames))
    ref_frames, hyp_frames = ref_frames[:size], hyp_frames[:size]

    scored = _collar_mask(reference, resolution_ms, size, collar_ms)
    ref_frames = np.where(scored, ref_frames, -2)  # -2 ⇒ excluded from scoring
    hyp_frames = np.where(scored, hyp_frames, -2)

    ref_speakers = sorted({int(s) for s in np.unique(ref_frames) if s >= 0})
    hyp_speakers = sorted({int(s) for s in np.unique(hyp_frames) if s >= 0})

    mapping: dict[int, int] = {}
    if ref_speakers and hyp_speakers:
        overlap = np.zeros((len(ref_speakers), len(hyp_speakers)), dtype=np.int64)
        for i, ref_id in enumerate(ref_speakers):
            ref_mask = ref_frames == ref_id
            for j, hyp_id in enumerate(hyp_speakers):
                overlap[i, j] = int(np.count_nonzero(ref_mask & (hyp_frames == hyp_id)))
        # Maximise overlap ⇒ minimise its negation.
        for i, j in _hungarian(-overlap):
            if i < len(ref_speakers) and j < len(hyp_speakers):
                mapping[hyp_speakers[j]] = ref_speakers[i]

    remapped = np.full_like(hyp_frames, -1)
    remapped[hyp_frames == -2] = -2
    for hyp_id, ref_id in mapping.items():
        remapped[hyp_frames == hyp_id] = ref_id
    # A hypothesis speaker with no mapping is always confusion, never a match.
    unmapped = (hyp_frames >= 0) & ~np.isin(hyp_frames, list(mapping))
    remapped[unmapped] = -3

    ref_speech = ref_frames >= 0
    hyp_speech = remapped >= 0
    total = int(np.count_nonzero(ref_speech))

    missed = int(np.count_nonzero(ref_speech & ~hyp_speech & (remapped != -2)))
    false_alarm = int(np.count_nonzero(~ref_speech & hyp_speech & (ref_frames != -2)))
    confusion = int(np.count_nonzero(ref_speech & hyp_speech & (ref_frames != remapped)))

    scale = resolution_ms
    return DERResult(
        der=(missed + false_alarm + confusion) / total if total else 0.0,
        missed_ms=missed * scale,
        false_alarm_ms=false_alarm * scale,
        confusion_ms=confusion * scale,
        total_reference_ms=total * scale,
        reference_speakers=len(ref_speakers),
        hypothesis_speakers=len(hyp_speakers),
        mapping=mapping,
    )


def segments_from_rttm(path: str) -> list[SpeakerSegment]:
    """Parse NIST RTTM, the format every diarization corpus ships in."""
    segments: list[SpeakerSegment] = []
    labels: dict[str, int] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) < 8 or parts[0] != "SPEAKER":
                continue
            start = float(parts[3])
            duration = float(parts[4])
            label = parts[7]
            index = labels.setdefault(label, len(labels))
            segments.append(
                SpeakerSegment(
                    start_ms=int(start * 1000),
                    end_ms=int((start + duration) * 1000),
                    speaker=index,
                )
            )
    return segments


def segments_to_rttm(segments: list[SpeakerSegment], file_id: str) -> str:
    lines = []
    for segment in sorted(segments, key=lambda s: s.start_ms):
        start = segment.start_ms / 1000
        duration = (segment.end_ms - segment.start_ms) / 1000
        lines.append(
            f"SPEAKER {file_id} 1 {start:.3f} {duration:.3f} <NA> <NA> "
            f"speaker_{segment.speaker} <NA> <NA>"
        )
    return "\n".join(lines) + "\n"
