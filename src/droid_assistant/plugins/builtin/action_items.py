"""Action items — reference plugin (FR-PLG-11).

Emits two artifacts from one run: rendered Markdown for reading, and JSON for
anything downstream that wants structure. The JSON is the reason a plugin can
emit more than once per handler.

The prompt is built around the failure mode that makes this kind of feature
useless: inventing commitments nobody made. A short list of real action items
beats a long list containing three plausible fabrications.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ...domain import Artifact
from ..api import Context, Event, Plugin, format_transcript

SYSTEM = """\
You extract action items from meeting transcripts produced by automatic speech \
recognition.

Rules:
- Extract only commitments that were actually made. Do not infer, suggest, or \
invent tasks.
- An owner is recorded only when the transcript names one. Otherwise it is null.
- A due date is recorded only when one is stated. Relative dates ("by Friday") \
are kept verbatim.
- If nothing was committed to, return an empty array. That is a valid answer and \
usually the correct one for a short conversation.
- Return ONLY a JSON array. No prose, no code fence.

Each object: {"text": str, "owner": str|null, "due": str|null, "quote": str}
`quote` is the transcript line the item came from, so a reader can check it.\
"""


class ActionItemsConfig(BaseModel):
    max_items: int = Field(default=20, ge=1, le=100)
    include_quotes: bool = Field(
        default=True, description="Show the transcript line each item came from"
    )
    also_emit_json: bool = Field(
        default=True, description="Emit a machine-readable artifact alongside the Markdown"
    )


class ActionItemsPlugin(Plugin):
    name = "action_items"
    version = "1.0.0"
    api_version = 1
    description = "Extracts commitments, owners, and due dates"
    config_schema = ActionItemsConfig
    subscribes = {Event.SESSION_END}
    requires_llm = True

    async def on_session_end(self, ctx: Context) -> Artifact | None:
        config: ActionItemsConfig = ctx.config
        utterances = await ctx.store.utterances(ctx.session_id)
        if not utterances:
            return None
        speakers = {s.id: s for s in await ctx.store.speakers(ctx.session_id)}
        transcript = format_transcript(utterances, speakers, include_translation=True)

        raw = await ctx.llm.complete(
            f"Extract at most {config.max_items} action items.\n\nTranscript:\n{transcript}",
            system=SYSTEM,
            temperature=0.0,
        )
        items = _parse(raw)
        if config.also_emit_json:
            await ctx.emit_artifact(
                Artifact(
                    kind="action_items_json",
                    mime="application/json",
                    content=json.dumps(items, ensure_ascii=False, indent=2),
                    metadata={"count": len(items)},
                )
            )
        return Artifact(
            kind="action_items",
            mime="text/markdown",
            content=_render(items, include_quotes=config.include_quotes),
            metadata={"count": len(items)},
        )


def _parse(raw: str) -> list[dict[str, Any]]:
    """Tolerate a code fence or a leading sentence; refuse to guess beyond that."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in parsed:
        if isinstance(entry, dict) and str(entry.get("text", "")).strip():
            items.append(
                {
                    "text": str(entry["text"]).strip(),
                    "owner": _clean(entry.get("owner")),
                    "due": _clean(entry.get("due")),
                    "quote": _clean(entry.get("quote")),
                }
            )
    return items


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in {"", "null", "none", "n/a", "unknown"} else text


def _render(items: list[dict[str, Any]], *, include_quotes: bool) -> str:
    if not items:
        return "_No action items were committed to in this session._"
    lines = ["## Action items", ""]
    for item in items:
        suffix = []
        if item["owner"]:
            suffix.append(f"**{item['owner']}**")
        if item["due"]:
            suffix.append(f"_{item['due']}_")
        tail = f" — {' · '.join(suffix)}" if suffix else ""
        lines.append(f"- [ ] {item['text']}{tail}")
        if include_quotes and item["quote"]:
            lines.append(f"  > {item['quote']}")
    return "\n".join(lines)
