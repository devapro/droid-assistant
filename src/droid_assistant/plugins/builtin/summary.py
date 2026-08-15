"""Meeting summary — reference plugin (FR-PLG-10).

Also the worked example the plugin documentation points at, so it is written the
way a third-party plugin should be: config as a pydantic model, one handler, an
Artifact returned, no credential handling, and no assumption that the LLM is
available.

**On demand, not on every session.** Summarising costs a whole-transcript LLM
call, and most recordings are never read a second time. Spending that
automatically bills for summaries nobody asked for and — where the credential is
a cloud one — sends every conversation to a provider as a matter of course. So
the Summary tab offers a button instead, and the decision is made per recording.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...domain import Artifact
from ..api import Context, Event, Plugin, format_transcript

SYSTEM = """\
You summarise meeting transcripts. The transcript comes from automatic speech \
recognition, so it contains errors, and speaker labels may be wrong. Write what \
the transcript supports and nothing more. Never invent a decision, a name, or a \
number. Where the transcript is unclear, say so rather than guessing.\
"""


class SummaryConfig(BaseModel):
    """Rendered as the settings form for this plugin (FR-PLG-5)."""

    style: str = Field(
        default="bullets",
        description="bullets, prose, or minutes",
        json_schema_extra={"enum": ["bullets", "prose", "minutes"]},
    )
    max_words: int = Field(default=400, ge=50, le=2000, description="Length ceiling")
    include_decisions: bool = Field(default=True, description="Call out decisions separately")
    include_translation: bool = Field(
        default=False,
        description="Summarise from translations rather than the original language",
    )
    min_utterances: int = Field(
        default=5,
        ge=1,
        description="Skip sessions shorter than this — a voice note is not a meeting",
    )


_STYLE_INSTRUCTION = {
    "bullets": "Write short bullet points grouped under topic headings.",
    "prose": "Write two or three short paragraphs of continuous prose.",
    "minutes": (
        "Write formal minutes: Attendees, Discussion, Decisions, Next steps. "
        "Omit any section the transcript does not support."
    ),
}


class SummaryPlugin(Plugin):
    name = "summary"
    version = "1.0.0"
    api_version = 1
    description = "Summarises a conversation, when you ask for one"
    config_schema = SummaryConfig
    #: Which handler an on-demand run reaches. It is never dispatched, because…
    subscribes = {Event.SESSION_END}
    requires_llm = True
    on_demand = True  # …see the module docstring

    async def on_session_end(self, ctx: Context) -> Artifact | None:
        config: SummaryConfig = ctx.config
        utterances = await ctx.utterances()
        if not utterances:
            ctx.decline("There is nothing to summarise — this recording has no transcript.")
            return None
        # The floor exists to stop a voice note being summarised on the way past.
        # Somebody who pressed the button has already answered that question, and
        # refusing them is a control that does nothing for no stated reason.
        if len(utterances) < config.min_utterances and not ctx.requested:
            ctx.decline(
                f"Only {len(utterances)} lines — shorter than the {config.min_utterances} "
                "this plugin summarises automatically."
            )
            return None

        speakers = {s.id: s for s in await ctx.store.speakers(ctx.session_id)}
        transcript = format_transcript(
            utterances,
            speakers,
            include_speakers=True,
            include_translation=config.include_translation,
        )
        metadata = await ctx.store.session_metadata(ctx.session_id)

        instruction = _STYLE_INSTRUCTION.get(config.style, _STYLE_INSTRUCTION["bullets"])
        decisions = (
            "\nList decisions separately under a **Decisions** heading. "
            "If none were made, say so in one line."
            if config.include_decisions
            else ""
        )
        prompt = (
            f"{instruction}\n"
            f"Use at most {config.max_words} words.{decisions}\n"
            f"Write in English regardless of the transcript's language.\n\n"
            f"Session: {metadata.get('title') or 'untitled'}\n"
            f"Duration: {metadata.get('duration_ms', 0) // 60000} minutes, "
            f"{len(speakers)} speaker(s)\n\n"
            f"Transcript:\n{transcript}"
        )

        text = await ctx.llm.complete(prompt, system=SYSTEM, max_tokens=config.max_words * 3)
        if not text.strip():
            return None
        return Artifact(
            kind="summary",
            mime="text/markdown",
            content=text.strip(),
            metadata={
                "style": config.style,
                "utterances": len(utterances),
                "speakers": len(speakers),
            },
        )
