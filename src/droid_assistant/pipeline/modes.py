"""Latency-mode profiles (FR-LAT-1 … FR-LAT-7).

One pipeline graph, three parameterisations. Keeping the modes as data rather
than three code paths is what makes mid-session switching tractable (FR-LAT-3):
switching swaps a profile object at a VAD boundary instead of tearing down and
rebuilding a pipeline.

    Live      sliding window + LocalAgreement-2; partials, revised in place
    Balanced  commit once on VAD endpoint; no partials, text only ever appended
    Batch     nothing until stop; then the whole session at once

All three assemble *turns*: consecutive segments from one speaker grow a single
message rather than each becoming its own. Live is no exception, though it reads
like one — LocalAgreement governs the words inside a segment and says nothing
about which message the segment belongs to, so without turn assembly a Live
transcript is a column of one-sentence messages with the same name over each.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain import LatencyMode


@dataclass(slots=True, frozen=True)
class ModeProfile:
    mode: LatencyMode
    emits_partials: bool
    transcribes_live: bool
    #: How often Live mode re-decodes its window. Below ~500 ms the decode cost
    #: dominates and latency gets worse, not better. Enforced in
    #: `orchestrator._live_tick`, which its callers reach far more often than
    #: this — the loop runs per ingest frame, and the step is what turns that
    #: into a cadence a local model can hold.
    window_step_ms: int = 800
    #: The most audio one Live decode covers. Longer is more accurate and slower;
    #: this is the main knob R2 measures.
    window_max_ms: int = 15_000
    #: Diarization granularity in the live modes: how much context around an
    #: utterance the diarizer sees. Too little and speaker clustering has nothing
    #: to work with — and it must cover a whole segment, so `vad.max_speech_ms`
    #: is the floor, not a target. Costs one diarizer pass per endpoint, which
    #: Live pays inside its latency budget; `diarization.window_ms = 0` is the
    #: way out for a machine that cannot afford it.
    diarization_window_ms: int = 30_000
    latency_target_ms: int = 4_000

    @property
    def description(self) -> str:
        return _DESCRIPTIONS[self.mode]


LIVE = ModeProfile(
    mode=LatencyMode.LIVE,
    emits_partials=True,
    transcribes_live=True,
    window_step_ms=800,
    window_max_ms=15_000,
    latency_target_ms=2_500,  # NFR-PERF-1
)

BALANCED = ModeProfile(
    mode=LatencyMode.BALANCED,
    emits_partials=False,
    transcribes_live=True,
    latency_target_ms=4_000,  # NFR-PERF-2
)

BATCH = ModeProfile(
    mode=LatencyMode.BATCH,
    emits_partials=False,
    transcribes_live=False,
    diarization_window_ms=0,  # 0 ⇒ the whole session in one pass
    latency_target_ms=0,
)

PROFILES = {
    LatencyMode.LIVE: LIVE,
    LatencyMode.BALANCED: BALANCED,
    LatencyMode.BATCH: BATCH,
}

_DESCRIPTIONS = {
    LatencyMode.LIVE: (
        "Text appears as you speak and may be corrected in place. Highest quality bar and "
        "the highest cost: the recogniser re-runs over the last few seconds about once a "
        "second, so it needs a machine with room to spare."
    ),
    LatencyMode.BALANCED: (
        "Each sentence appears once you finish it, and never changes afterwards. "
        "The best default for most recording."
    ),
    LatencyMode.BATCH: (
        "Nothing is transcribed until you stop. Cheapest and most accurate, and the "
        "only mode that runs comfortably on low-powered hardware."
    ),
}


def profile_for(mode: LatencyMode) -> ModeProfile:
    return PROFILES[mode]


def describe(mode: LatencyMode, *, cloud_asr: bool, cloud_llm: bool) -> dict[str, object]:
    """What the UI shows next to a mode (FR-LAT-7): its latency target, what it
    costs, and whether choosing it means audio or text leaves the server.

    No mode is ever reported as unavailable, because none is: all three run on
    the same `transcribe` call. `redecodes_window` is the cost warning that
    replaced the availability flag — Live buys its partials by recognising the
    same audio repeatedly, which is a bill against a cloud backend and a load
    against a local one.
    """
    profile = profile_for(mode)
    return {
        "mode": str(mode),
        "description": profile.description,
        "latency_target_ms": profile.latency_target_ms,
        "emits_partials": profile.emits_partials,
        "uses_cloud_asr": cloud_asr,
        "uses_cloud_llm": cloud_llm,
        "redecodes_window": profile.emits_partials,
    }
