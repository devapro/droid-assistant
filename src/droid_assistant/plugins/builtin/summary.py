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

**The instructions are replaceable** (FR-PLG-14). `style` offers three shapes,
which covers the common cases and none of the specific ones: a support call, a
one-to-one, and a design review want different summaries, and no enum of ours is
going to guess them. A saved prompt takes the place of that block for one run.
What it does *not* replace is `SYSTEM`, because that is not a matter of taste —
it is what stops a summary inventing a decision the meeting never made, and a
custom prompt is no reason to drop the guard.
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
        description="bullets, prose, or minutes — ignored when a saved prompt is chosen",
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
    accepts_prompt = True  # FR-PLG-14

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

        # A saved prompt replaces the whole instruction block, `style` and the
        # decisions heading with it: somebody who wrote "list the customer's
        # objections, then what we promised" does not also want three bullet
        # headings they did not ask for. The output language goes with it — the
        # built-in prompt pins English, and a prompt written in Russian asking
        # for a Russian summary would otherwise be contradicted a line later.
        instruction = (
            ctx.prompt.instructions.strip()
            if ctx.prompt is not None
            else (
                f"{_STYLE_INSTRUCTION.get(config.style, _STYLE_INSTRUCTION['bullets'])}\n"
                + (
                    "List decisions separately under a **Decisions** heading. "
                    "If none were made, say so in one line.\n"
                    if config.include_decisions
                    else ""
                )
                + "Write in English regardless of the transcript's language."
            )
        )
        prompt = (
            f"{instruction}\n"
            # The length ceiling survives a custom prompt because it is not a
            # style choice — it is what bounds the size of the reply, and of the
            # bill for it.
            f"Use at most {config.max_words} words.\n\n"
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
                "utterances": len(utterances),
                "speakers": len(speakers),
                # The prompt's *name*, not its id: the record has to still make
                # sense after the prompt has been edited or deleted. `style` is
                # recorded only when it was what shaped the output, because
                # "style: bullets" beside a custom prompt is a claim about this
                # summary that is not true of it.
                **(
                    {"prompt": ctx.prompt.name}
                    if ctx.prompt is not None
                    else {"style": config.style}
                ),
            },
        )
