"""Action items — reference plugin (FR-PLG-11).

Emits two artifacts from one run: rendered Markdown for reading, and JSON for
anything downstream that wants structure. The JSON is the reason a plugin can
emit more than once per handler.

The prompt is built around the failure mode that makes this kind of feature
useless: inventing commitments nobody made. A short list of real action items
beats a long list containing three plausible fabrications.

It also runs **scoped**, over one line the reader picked out rather than the
whole conversation — the plugin's answer to "make an action item out of that".
A scoped run adds to the list instead of replacing it, which is the only reading
of the button that survives pressing it twice.
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

#: The same job, asked of one line the reader deliberately picked out — and a
#: different question because of it. `SYSTEM` is written against the failure mode
#: of an unattended pass over a whole transcript: inventing commitments nobody
#: made. Applied to an explicit "make an action item out of *this*" it answers
#: the wrong question — it weighs whether the sentence qualifies, decides it is
#: only a remark, and returns nothing, so the button appears to do nothing. The
#: person clicking has already decided. The work left is to phrase it.
SYSTEM_SCOPED = """\
You turn a line of conversation into an action item. The line comes from \
automatic speech recognition, so it may be garbled or cut off mid-sentence.

The user has selected this line and asked for an action item from it. That \
decision is theirs and is already made — do not re-judge whether it is \
"really" a commitment, and do not decline because it is phrased loosely or as \
an aside. Write the task it implies.

Rules:
- Normally return exactly one item. Return more only if the line plainly \
contains several distinct tasks.
- Phrase it as an instruction, starting with a verb, in the language of the \
line. Keep it short.
- Stay inside what the line says. Do not add scope, detail, or a deadline that \
is not there.
- An owner is recorded only if the line names or clearly implies one; a due \
date only if one is stated. Otherwise null.
- Return an empty array ONLY if the line carries no action of any kind — a \
greeting, a filler word, an unintelligible fragment.
- Return ONLY a JSON array. No prose, no code fence.

Each object: {"text": str, "owner": str|null, "due": str|null, "quote": str}
`quote` is the line, verbatim.\
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
        utterances = await ctx.utterances()
        if not utterances:
            return None
        speakers = {s.id: s for s in await ctx.store.speakers(ctx.session_id)}
        transcript = format_transcript(utterances, speakers, include_translation=True)

        scoped = ctx.utterance_ids is not None
        instruction = (
            "Write the action item this line calls for."
            if scoped
            else f"Extract at most {config.max_items} action items."
        )
        raw = await ctx.llm.complete(
            f"{instruction}\n\n{'Line' if scoped else 'Transcript'}:\n{transcript}",
            system=SYSTEM_SCOPED if scoped else SYSTEM,
            temperature=0.0,
        )
        items = _parse(raw)
        added, linked = len(items), 0
        existing = await _previous_items(ctx)
        if scoped:
            # A scoped run *adds*: the operator picked one line, not a new list.
            # Replacing here would throw away everything the session-wide run
            # found, which is the opposite of what pressing the button on a
            # second message means. Each item records the line it came from, so
            # the transcript can show which lines are already tasks.
            source = ctx.utterance_ids[0] if ctx.utterance_ids else None
            items, added, linked = _merge(existing, items, source=source)
            items = items[: config.max_items]
            if not added and not linked:
                # Nothing changed. Emitting anyway would file a version
                # identical to the last one and, where the list could not be
                # read back, would replace it with a placeholder — losing items
                # the operator can see on screen.
                ctx.logger.info("scoped run changed nothing")
                return None
        else:
            # A whole-session pass refreshes what the *machine* found, and must
            # replace its own previous answer — that is what a re-run after a
            # transcript edit means (FR-SES-9). What it must never do is discard
            # what a person chose: items carrying a `source` were picked line by
            # line, usually while the conversation was still happening, and
            # dropping them at session end silently deleted the user's work.
            picked = [item for item in existing if item.get("source")]
            items, added, _ = _merge(picked, items)
            items = items[: config.max_items]

        # `added` and `linked` are what the UI reports back to whoever pressed
        # the button. `count` alone cannot tell "found one" from "found none and
        # there were none before", nor either of those from "it was already on
        # the list" — three outcomes a reader acts on differently.
        metadata = {"count": len(items), "added": added, "linked": linked}
        if config.also_emit_json:
            await ctx.emit_artifact(
                Artifact(
                    kind="action_items_json",
                    mime="application/json",
                    content=json.dumps(items, ensure_ascii=False, indent=2),
                    metadata=metadata,
                )
            )
        return Artifact(
            kind="action_items",
            mime="text/markdown",
            content=_render(items, include_quotes=config.include_quotes),
            metadata=metadata,
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


async def _previous_items(ctx: Context) -> list[dict[str, Any]]:
    """The list this run is adding to.

    The JSON artifact first, because it is the lossless record. Where
    `also_emit_json` is off there is none, and the rendered Markdown is read
    back instead — losing nothing a scoped run needs, and far better than the
    alternative, which was replacing a list the operator can see on screen with
    one built from a single line.
    """
    artifact = await ctx.previous("action_items_json")
    if artifact is not None:
        try:
            parsed = json.loads(artifact.get("content") or "[]")
        except json.JSONDecodeError:
            ctx.logger.warning("previous action_items_json did not parse; reading the Markdown")
        else:
            return [entry for entry in parsed if isinstance(entry, dict) and entry.get("text")]

    rendered = await ctx.previous("action_items")
    return _unrender(str(rendered.get("content") or "")) if rendered else []


#: One rendered item: `- [ ] text — **owner** · _due_`, with either half of the
#: suffix optional. Anchored to the checkbox so prose around the list is ignored.
_RENDERED = re.compile(r"^- \[[ x]\] (?P<text>.+?)(?: — (?P<suffix>.*))?$")


def _unrender(markdown: str) -> list[dict[str, Any]]:
    """Read `_render`'s own output back into records.

    A round trip through our own format, not a general Markdown parser: an item
    it cannot read is skipped rather than guessed at. Quotes are dropped — they
    exist so a reader can check an item, and nothing downstream keys on them.
    """
    items: list[dict[str, Any]] = []
    for line in markdown.splitlines():
        match = _RENDERED.match(line.strip())
        if match is None:
            continue
        owner = due = None
        for part in (match.group("suffix") or "").split(" · "):
            if part.startswith("**") and part.endswith("**"):
                owner = part[2:-2] or None
            elif part.startswith("_") and part.endswith("_"):
                due = part[1:-1] or None
        items.append(
            {"text": match.group("text").strip(), "owner": owner, "due": due, "quote": None}
        )
    return items


def _words(item: dict[str, Any]) -> frozenset[str]:
    """An item's text as a set of significant words."""
    text = str(item.get("text", "")).casefold()
    return frozenset(word for word in re.findall(r"\w+", text) if word not in _NOISE)


#: Words that carry no task. Kept deliberately short — this is for comparing two
#: phrasings of the same commitment, not for search.
_NOISE = frozenset({"a", "an", "the", "to", "by", "for", "of", "on", "in", "and", "with"})


def _merge(
    existing: list[dict[str, Any]], found: list[dict[str, Any]], *, source: str | None = None
) -> tuple[list[dict[str, Any]], int, int]:
    """Existing items first, then whatever is genuinely new.

    Returns the list, how many were added, and how many were *linked* — found
    to be already on it. Those are different answers and the reader acts on them
    differently: "added" sends them to the list, "already there" tells them the
    line is covered and they can move on.

    Two items are the same when one's words contain the other's. Exact text
    matching is not enough: the whole-session pass writes "Finish the migration"
    and a click on the line it came from writes "Finish the migration by
    Friday", and a list holding both is worse than either. Containment rather
    than equality also means the shorter phrasing already on the list wins,
    which keeps a repeated click from rewording an item under the reader.
    """
    merged = list(existing)
    words = [_words(item) for item in merged]
    added = linked = 0
    for item in found:
        new_words = _words(item)
        if not new_words:
            continue
        match = next(
            (i for i, other in enumerate(words) if new_words <= other or other <= new_words),
            None,
        )
        if match is None:
            merged.append({**item, **({"source": source} if source else {})})
            words.append(new_words)
            added += 1
        elif source and not merged[match].get("source"):
            # The item was already there, but nobody had said which line it came
            # from. This click did — so record it, and the transcript can mark
            # the line rather than leaving it looking untouched.
            merged[match] = {**merged[match], "source": source}
            linked += 1
    return merged, added, linked


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in {"", "null", "none", "n/a", "unknown"} else text


def _render(items: list[dict[str, Any]], *, include_quotes: bool) -> str:
    if not items:
        # Only ever reached by a whole-session run: a scoped one that finds
        # nothing returns without emitting, rather than filing this sentence
        # over a list somebody is looking at.
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
