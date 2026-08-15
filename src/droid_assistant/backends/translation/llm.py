"""LLM translation with rolling conversational context — the v1 default.

The prompt is deliberately plain and the constraints are deliberately explicit,
because the failure mode being designed against is not mistranslation. It is the
model *answering* the conversation, or summarising it, or helpfully expanding a
one-word reply into a sentence. Each instruction below maps to a specific way
that goes wrong in a live transcript.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from ..llm.base import LLMClient
from .base import TranslationBackend, TranslationCapabilities

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a translation engine inside a live transcription system. You translate \
one speaker turn at a time.

Rules:
- Output ONLY the translation. No preamble, no quotes, no notes, no explanation.
- Translate the marked turn only. Earlier turns are context for pronouns, \
gender, and register — never translate them.
- Preserve register and tone. Informal speech stays informal.
- Keep disfluencies and false starts if they carry meaning; drop pure filler.
- Never answer, continue, summarise, or comment on the conversation.
- If a turn is a fragment, translate the fragment. Do not complete it.
- Keep proper nouns, product names, and numbers exactly as they appear.
- If the turn is already in the target language, return it unchanged.\
"""

# Models that were about to preface the answer sometimes still do. Strip the
# handful of shapes that survive the system prompt.
_PREFIX = re.compile(r"^\s*(?:translation|перевод|prevod)\s*[:\-–]\s*", re.IGNORECASE)
_WRAPPING_QUOTES = re.compile(r'^\s*["“”«»](.*)["“”«»]\s*$', re.DOTALL)


def clean(text: str) -> str:
    text = _PREFIX.sub("", text.strip())
    match = _WRAPPING_QUOTES.match(text)
    if match and '"' not in match.group(1):
        text = match.group(1)
    return text.strip()


LANGUAGE_NAMES = {
    "en": "English",
    "ru": "Russian",
    "sr": "Serbian",
    "hr": "Croatian",
    "bs": "Bosnian",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "uk": "Ukrainian",
    "pl": "Polish",
    "tr": "Turkish",
    "zh": "Chinese",
    "ja": "Japanese",
}


def language_name(code: str | None) -> str:
    if not code:
        return "the source language"
    return LANGUAGE_NAMES.get(code.split("-")[0].lower(), code)


class LLMTranslationBackend(TranslationBackend):
    def __init__(self, client: LLMClient, context_utterances: int = 6) -> None:
        self._client = client
        self._context_n = context_utterances

    @property
    def capabilities(self) -> TranslationCapabilities:
        return TranslationCapabilities(
            name=f"llm:{self._client.capabilities.model}",
            local=self._client.capabilities.local,
            uses_context=True,
            languages=None,
        )

    def _prompt(self, text: str, src: str | None, dst: str, context: Sequence[str]) -> str:
        parts = [f"Translate from {language_name(src)} into {language_name(dst)}."]
        recent = list(context)[-self._context_n :]
        if recent:
            parts.append("\nPreceding turns, for context only — do not translate them:")
            parts.extend(f"  {line}" for line in recent)
        parts.append("\nTranslate this turn:")
        parts.append(text)
        return "\n".join(parts)

    async def translate(
        self, text: str, src: str | None, dst: str, context: Sequence[str] = ()
    ) -> str:
        if not text.strip():
            return ""
        raw = await self._client.complete(
            self._prompt(text, src, dst, context), system=SYSTEM_PROMPT, temperature=0.1
        )
        return clean(raw)

    async def translate_batch(
        self, texts: Sequence[str], src: str | None, dst: str, context: Sequence[str] = ()
    ) -> list[str]:
        """FR-TRA-9. One request, JSON in and JSON out.

        A rapid exchange of one-word turns otherwise issues one request per word,
        which costs more in latency than the turns are worth. On any parsing
        failure we fall back to translating individually rather than returning
        something misaligned — a translation attached to the wrong utterance is
        worse than a slow one.
        """
        items = list(texts)
        if len(items) <= 1:
            return [await self.translate(t, src, dst, context) for t in items]

        numbered = json.dumps(
            [{"i": i, "text": t} for i, t in enumerate(items)], ensure_ascii=False
        )
        recent = list(context)[-self._context_n :]
        prompt = (
            f"Translate each turn from {language_name(src)} into {language_name(dst)}.\n"
            + (
                "\nPreceding turns, for context only:\n"
                + "\n".join(f"  {line}" for line in recent)
                + "\n"
                if recent
                else ""
            )
            + "\nReturn ONLY a JSON array of objects with keys `i` and `text`, one per input, "
            "in the same order and with the same `i` values.\n\nInput:\n" + numbered
        )
        try:
            raw = await self._client.complete(prompt, system=SYSTEM_PROMPT, temperature=0.1)
            parsed = json.loads(_strip_fence(raw))
            by_index = {int(row["i"]): clean(str(row["text"])) for row in parsed}
            if set(by_index) != set(range(len(items))):
                raise ValueError("indices did not round-trip")
            return [by_index[i] for i in range(len(items))]
        except Exception as exc:
            log.debug("batch translation fell back to sequential: %s", exc)
            return [await self.translate(t, src, dst, context) for t in items]


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
    return stripped.strip()
