"""Meeting summary — reference plugin (FR-PLG-10).

Also the worked example the plugin documentation points at, so it is written the
way a third-party plugin should be: config as a pydantic model, one handler, an
Artifact returned, no credential handling, and no assumption that the LLM is
available.
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
    description = "Summarises the session once it ends"
    config_schema = SummaryConfig
    subscribes = {Event.SESSION_END}
    requires_llm = True

    async def on_session_end(self, ctx: Context) -> Artifact | None:
        config: SummaryConfig = ctx.config
        utterances = await ctx.store.utterances(ctx.session_id)
        if len(utterances) < config.min_utterances:
            ctx.logger.info("session too short to summarise", extra={"utterances": len(utterances)})
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
