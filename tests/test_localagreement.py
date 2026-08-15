"""LocalAgreement-2 (FR-LAT-4, R2).

The policy has one job: commit a prefix only when two consecutive decodes agree
on it, so committed text does not visibly rewrite itself. These tests pin the
three behaviours that make it usable rather than merely correct — tolerance of
punctuation churn between decodes, never dropping the tail of an utterance, and
never un-committing text the user has already read.
"""

from __future__ import annotations

from droid_assistant.domain import ASRResult, Word
from droid_assistant.pipeline.localagreement import LocalAgreement, merge_results, normalise


def hypothesis(text: str, start_ms: int = 0) -> ASRResult:
    tokens = text.split()
    step = 200
    words = [
        Word(w=token, start_ms=start_ms + i * step, end_ms=start_ms + (i + 1) * step)
        for i, token in enumerate(tokens)
    ]
    return ASRResult(
        text=text, start_ms=start_ms, end_ms=start_ms + len(tokens) * step, words=words
    )


def test_nothing_commits_from_a_single_decode() -> None:
    agreement = LocalAgreement()
    commit, partial = agreement.update(hypothesis("we need to"))
    assert commit is None  # nothing to agree with yet
    assert partial == "we need to"


def test_agreeing_prefix_commits() -> None:
    agreement = LocalAgreement()
    agreement.update(hypothesis("we need to finish"))
    commit, partial = agreement.update(hypothesis("we need to finish this"))
    assert commit is not None
    assert commit.text == "we need to finish"
    assert partial == "we need to finish this"


def test_disagreement_stops_the_commit_at_the_divergence() -> None:
    agreement = LocalAgreement()
    agreement.update(hypothesis("we need to finnish"))
    commit, _ = agreement.update(hypothesis("we need to finish this"))
    assert commit is not None
    assert commit.text == "we need to"  # stops where the decodes diverge


def test_punctuation_and_case_do_not_block_agreement() -> None:
    """Two decodes of the same audio routinely differ only by a comma.

    Treating that as disagreement would stall the commit point and make Live
    mode feel broken, so comparison is on normalised tokens.
    """
    agreement = LocalAgreement()
    agreement.update(hypothesis("we need to finish"))
    commit, _ = agreement.update(hypothesis("We need, to finish this"))
    assert commit is not None
    assert len(commit.words) == 4
    # The newer decode's text is kept — better punctuation, same words.
    assert commit.words[0].w == "We"


def test_committed_text_is_never_retracted() -> None:
    """The model changing its mind about settled text must not flip the UI.

    Showing a small error is better than text that rewrites itself after the
    reader has moved on.
    """
    agreement = LocalAgreement()
    agreement.update(hypothesis("we need to finish"))
    agreement.update(hypothesis("we need to finish"))
    assert agreement.committed_text == "we need to finish"

    # A later decode disagrees about the beginning.
    _commit, partial = agreement.update(hypothesis("we needed to finish this"))
    assert agreement.committed_text.startswith("we need to finish")
    assert "we need to finish" in partial


def test_flush_commits_the_tail_unconditionally() -> None:
    """At the end of a speech region there will be no further hypothesis to
    agree with, so waiting for agreement would drop the last words of every
    single utterance."""
    agreement = LocalAgreement()
    agreement.update(hypothesis("we need to finish"))
    agreement.update(hypothesis("we need to finish this by friday"))
    tail = agreement.flush()
    assert tail is not None
    assert tail.text == "this by friday"
    assert agreement.committed_text == "we need to finish this by friday"


def test_flush_on_an_empty_window_returns_nothing() -> None:
    assert LocalAgreement().flush() is None


def test_reset_clears_state_between_segments() -> None:
    agreement = LocalAgreement()
    agreement.update(hypothesis("first utterance"))
    agreement.update(hypothesis("first utterance"))
    agreement.reset()
    commit, partial = agreement.update(hypothesis("second utterance"))
    assert commit is None
    assert partial == "second utterance"


def test_normalise_strips_punctuation_and_case() -> None:
    assert normalise("Friday,") == normalise("friday") == "friday"
    assert normalise("don't") == "don't"  # an apostrophe is part of the word


def test_merge_results_flattens_a_multi_segment_decode() -> None:
    merged = merge_results([hypothesis("we need"), hypothesis("to finish", start_ms=400)])
    assert merged is not None
    assert merged.text == "we need to finish"
    assert len(merged.words) == 4


def test_merge_results_synthesises_words_when_absent() -> None:
    """Backends without word timings still need tokens: agreement is computed
    over words, and `whisper.cpp` reports only segments."""
    bare = ASRResult(text="we need to finish", start_ms=0, end_ms=800, words=[])
    merged = merge_results([bare])
    assert merged is not None
    assert [w.w for w in merged.words] == ["we", "need", "to", "finish"]
    assert merged.words[0].start_ms == 0
    assert merged.words[-1].end_ms <= 800


def test_merge_results_ignores_empty_segments() -> None:
    assert merge_results([ASRResult(text="   ", start_ms=0, end_ms=100)]) is None
    assert merge_results([]) is None
