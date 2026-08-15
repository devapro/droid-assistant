"""Word Error Rate (NFR-EVAL-1).

Normalisation is the part that decides whether a WER figure means anything.
Comparing raw strings punishes a model for punctuation and casing it was never
asked to get right, and inflates every number by several points — which then
gets compared against published figures that normalised differently.

The convention here follows Whisper's own English normaliser closely enough to
be comparable, and extends it where this project's languages need it:

  * case folded, punctuation stripped, whitespace collapsed
  * numbers left alone — "20" and "twenty" are genuinely different outputs, and
    silently equating them hides a real failure mode
  * Serbian transliterated to Latin before comparison, so a Cyrillic reference
    and a Latin hypothesis are not scored as 100% wrong (FR-ASR-10)
  * Russian ё → е, which is inconsistently written by humans and models alike
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from droid_assistant.backends.asr.base import normalise_serbian

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")

CYRILLIC_SERBIAN = set("ђћџљњ")


def normalise(text: str, language: str | None = None) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    base = (language or "").split("-")[0]

    if base in {"sr", "hr", "bs"} or (CYRILLIC_SERBIAN & set(text)):
        text = normalise_serbian(text, "latin")
    if base == "ru":
        text = text.replace("ё", "е")

    text = _PUNCT.sub(" ", text)
    return _SPACE.sub(" ", text).strip()


def tokenise(text: str, language: str | None = None) -> list[str]:
    normalised = normalise(text, language)
    return normalised.split() if normalised else []


@dataclass(slots=True, frozen=True)
class WERResult:
    wer: float
    substitutions: int
    deletions: int
    insertions: int
    hits: int
    reference_words: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    def to_json(self) -> dict[str, float | int]:
        return {
            "wer": round(self.wer, 4),
            "substitutions": self.substitutions,
            "deletions": self.deletions,
            "insertions": self.insertions,
            "hits": self.hits,
            "reference_words": self.reference_words,
        }


def word_error_rate(reference: str, hypothesis: str, language: str | None = None) -> WERResult:
    """Levenshtein over words, with the edit types broken out.

    The breakdown matters more than the single number: deletions dominating
    means audio is being lost or VAD is over-gating, while substitutions
    dominating means the model is mishearing. Those need different fixes.
    """
    ref = tokenise(reference, language)
    hyp = tokenise(hypothesis, language)

    if not ref:
        return WERResult(
            wer=1.0 if hyp else 0.0,
            substitutions=0,
            deletions=0,
            insertions=len(hyp),
            hits=0,
            reference_words=0,
        )

    # Full DP table: O(len(ref) × len(hyp)) memory, which for a ten-minute clip
    # is a few million cells — acceptable, and it lets us backtrace edit types.
    rows, cols = len(ref) + 1, len(hyp) + 1
    dist = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        dist[i][0] = i
    for j in range(cols):
        dist[0][j] = j

    for i in range(1, rows):
        ref_word = ref[i - 1]
        row, previous = dist[i], dist[i - 1]
        for j in range(1, cols):
            if ref_word == hyp[j - 1]:
                row[j] = previous[j - 1]
            else:
                row[j] = 1 + min(previous[j - 1], previous[j], row[j - 1])

    substitutions = deletions = insertions = hits = 0
    i, j = len(ref), len(hyp)
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1] and dist[i][j] == dist[i - 1][j - 1]:
            hits += 1
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and dist[i][j] == dist[i - 1][j - 1] + 1:
            substitutions += 1
            i, j = i - 1, j - 1
        elif i > 0 and dist[i][j] == dist[i - 1][j] + 1:
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1

    return WERResult(
        wer=(substitutions + deletions + insertions) / len(ref),
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
        hits=hits,
        reference_words=len(ref),
    )


def corpus_wer(pairs: list[tuple[str, str]], language: str | None = None) -> WERResult:
    """Aggregate over a corpus by summing errors and reference words.

    Averaging per-clip WER would weight a five-word clip the same as a
    five-minute one, which is how a corpus number ends up dominated by its
    shortest files.
    """
    totals = [0, 0, 0, 0, 0]
    for reference, hypothesis in pairs:
        result = word_error_rate(reference, hypothesis, language)
        totals[0] += result.substitutions
        totals[1] += result.deletions
        totals[2] += result.insertions
        totals[3] += result.hits
        totals[4] += result.reference_words
    substitutions, deletions, insertions, hits, words = totals
    return WERResult(
        wer=(substitutions + deletions + insertions) / words if words else 0.0,
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
        hits=hits,
        reference_words=words,
    )
