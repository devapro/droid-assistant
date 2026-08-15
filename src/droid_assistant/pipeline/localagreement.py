"""LocalAgreement-2 over a batch ASR model — what makes Live mode possible.

Whisper is not a streaming model. The policy (Macháček et al., 2023) works
around that: re-run the model over a growing window, and commit only the prefix
that two consecutive hypotheses agree on. Agreement between independent decodes
is a good proxy for stability, so committed text rarely needs revision, while
the uncommitted tail is shown as a partial that may rewrite.

The cost is honest: every window is decoded more than once, which is the 2–3×
compute overhead that caps this approach on weak hardware. R2 exists to measure
whether the resulting latency meets NFR-PERF-1 locally for Russian and Serbian.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..domain import ASRResult, Word

log = logging.getLogger(__name__)

_PUNCT = re.compile(r"[^\w']+", re.UNICODE)


def normalise(token: str) -> str:
    """Compare tokens ignoring case and punctuation.

    Two decodes of the same audio routinely differ only by a comma or a
    capitalisation. Treating those as disagreement would stall the commit point
    and make Live mode feel broken.
    """
    return _PUNCT.sub("", token.lower())


@dataclass(slots=True)
class Commit:
    """Newly stable text, with the timing of its final word."""

    text: str
    words: list[Word]

    @property
    def end_ms(self) -> int:
        return self.words[-1].end_ms if self.words else 0


@dataclass
class LocalAgreement:
    """State for one continuous speech region.

    `n` is fixed at 2 in the literature and here: three-way agreement commits so
    late that the latency saving disappears.
    """

    committed: list[Word] = field(default_factory=list)
    _previous: list[Word] = field(default_factory=list)

    def reset(self) -> None:
        self.committed.clear()
        self._previous.clear()

    @property
    def committed_text(self) -> str:
        return " ".join(w.w for w in self.committed)

    @property
    def committed_end_ms(self) -> int:
        return self.committed[-1].end_ms if self.committed else 0

    def update(self, hypothesis: ASRResult) -> tuple[Commit | None, str]:
        """Feed a fresh decode of the current window.

        Returns `(newly committed text or None, the full partial text)`. The
        partial is always the complete current hypothesis — committed prefix plus
        unstable tail — because that is what the UI renders in place.
        """
        words = list(hypothesis.words) or _synthesise_words(hypothesis)
        if not words:
            return None, self.committed_text

        # The hypothesis covers the whole window, so it re-states what we have
        # already committed. Skip that prefix before looking for agreement.
        offset = self._skip_committed(words)
        tail = words[offset:]

        agreed = _common_prefix(self._previous, tail)
        self._previous = tail

        commit: Commit | None = None
        if agreed:
            self.committed.extend(agreed)
            commit = Commit(text=" ".join(w.w for w in agreed), words=agreed)
            self._previous = tail[len(agreed) :]

        partial = " ".join(w.w for w in [*self.committed, *self._previous])
        return commit, partial.strip()

    def _skip_committed(self, words: list[Word]) -> int:
        """How many leading hypothesis words restate already-committed text."""
        if not self.committed:
            return 0
        i = j = 0
        while i < len(self.committed) and j < len(words):
            if normalise(self.committed[i].w) == normalise(words[j].w):
                i += 1
                j += 1
            else:
                # A disagreement inside committed text: the model changed its
                # mind about something we already showed as settled. Keep our
                # version — flip-flopping settled text is worse than a small
                # error — and resynchronise on timing.
                j += 1
        return j

    def flush(self) -> Commit | None:
        """End of speech region: commit whatever is left, unconditionally.

        There will be no further hypothesis to agree with, so waiting for
        agreement here would simply drop the last few words of every utterance.
        """
        if not self._previous:
            return None
        tail = self._previous
        self.committed.extend(tail)
        self._previous = []
        return Commit(text=" ".join(w.w for w in tail), words=tail)


def _common_prefix(a: list[Word], b: list[Word]) -> list[Word]:
    out: list[Word] = []
    for x, y in zip(a, b, strict=False):
        if normalise(x.w) != normalise(y.w) or not normalise(x.w):
            break
        # Keep the newer decode's text (better punctuation) with its own timing.
        out.append(y)
    return out


def _synthesise_words(result: ASRResult) -> list[Word]:
    """Spread a segment's text evenly across its span.

    Needed for backends that report no word timings (`whisper.cpp`): agreement
    is computed over tokens, so we need tokens even if the timing is
    approximate. Timing accuracy here affects seek precision, not correctness.
    """
    tokens = result.text.split()
    if not tokens:
        return []
    span = max(1, result.end_ms - result.start_ms)
    step = span / len(tokens)
    return [
        Word(
            w=token,
            start_ms=result.start_ms + int(i * step),
            end_ms=result.start_ms + int((i + 1) * step),
        )
        for i, token in enumerate(tokens)
    ]


def merge_results(results: list[ASRResult]) -> ASRResult | None:
    """Flatten a multi-segment decode of one window into a single hypothesis."""
    usable = [r for r in results if r.text.strip()]
    if not usable:
        return None
    words: list[Word] = []
    for result in usable:
        words.extend(result.words or _synthesise_words(result))
    speakers = {r.speaker for r in usable if r.speaker is not None}
    return ASRResult(
        text=" ".join(r.text.strip() for r in usable),
        start_ms=usable[0].start_ms,
        end_ms=usable[-1].end_ms,
        language=usable[0].language,
        confidence=min((r.confidence for r in usable if r.confidence is not None), default=None),
        words=words,
        # Merging two speakers into one line would attribute half of it to the
        # wrong person; no label is better than a wrong one (FR-DIA-10).
        speaker=speakers.pop() if len(speakers) == 1 else None,
    )
