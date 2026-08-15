"""Local NMT via CTranslate2 — the offline translation fallback (FR-TRA-6).

Quality is below the LLM path, and it is context-free by construction: an
encoder-decoder NMT model translates one sentence in isolation, which is exactly
the pronoun and gender problem FR-TRA-3 exists to avoid. That trade is stated in
`capabilities.uses_context = False`, and the UI can say so.

Default weights are Helsinki-NLP Opus-MT, which is permissively licensed and
therefore shippable as an open-source default. NLLB-200 is better and CC-BY-NC,
so it stays an opt-in the operator configures deliberately (C-6).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ...config import TranslationConfig
from .base import TranslationBackend, TranslationCapabilities

log = logging.getLogger(__name__)

# Opus-MT multilingual models select the target with a leading token.
_TARGET_TOKEN = {
    "en": ">>eng<<",
    "ru": ">>rus<<",
    "sr": ">>srp<<",
    "de": ">>deu<<",
    "fr": ">>fra<<",
}


class CTranslate2Backend(TranslationBackend):
    def __init__(self, config: TranslationConfig, models_dir: Path) -> None:
        self._config = config
        self._dir = models_dir / config.local_model_dir
        self._translator: Any = None
        self._tokenizer: Any = None
        self._lock = asyncio.Semaphore(1)

    @property
    def capabilities(self) -> TranslationCapabilities:
        return TranslationCapabilities(
            name=f"ctranslate2:{self._config.local_model}",
            local=True,
            uses_context=False,
            languages=None,
        )

    async def load(self) -> None:
        if self._translator is not None:
            return
        try:
            import ctranslate2
            from transformers import AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "translation.backend = 'ctranslate2' needs: uv sync --extra local"
            ) from exc
        if not self._dir.exists():
            raise RuntimeError(
                f"local translation model not found at {self._dir}.\n"
                "Fetch and convert it with:  droid-assistant models download --translation"
            )

        def build() -> tuple[Any, Any]:
            translator = ctranslate2.Translator(str(self._dir), device="cpu", compute_type="int8")
            tokenizer = AutoTokenizer.from_pretrained(self._config.local_model)
            return translator, tokenizer

        self._translator, self._tokenizer = await asyncio.to_thread(build)

    async def close(self) -> None:
        self._translator = self._tokenizer = None

    async def translate(
        self, text: str, src: str | None, dst: str, context: Sequence[str] = ()
    ) -> str:
        results = await self.translate_batch([text], src, dst, context)
        return results[0] if results else ""

    async def translate_batch(
        self, texts: Sequence[str], src: str | None, dst: str, context: Sequence[str] = ()
    ) -> list[str]:
        if self._translator is None:
            await self.load()
        items = [t for t in texts]
        if not any(t.strip() for t in items):
            return ["" for _ in items]

        token = _TARGET_TOKEN.get(dst.split("-")[0].lower(), "")

        def run() -> list[str]:
            batch = [
                self._tokenizer.convert_ids_to_tokens(
                    self._tokenizer.encode(f"{token} {text}".strip())
                )
                for text in items
            ]
            results = self._translator.translate_batch(batch, beam_size=4, max_decoding_length=512)
            return [
                self._tokenizer.decode(
                    self._tokenizer.convert_tokens_to_ids(r.hypotheses[0]),
                    skip_special_tokens=True,
                ).strip()
                for r in results
            ]

        async with self._lock:
            return await asyncio.to_thread(run)


class IdentityTranslationBackend(TranslationBackend):
    """Returns the source text. Used by tests, and as the last fallback when
    translation is enabled but every backend is unavailable — the transcript
    still renders, with translation marked failed rather than the session
    breaking."""

    @property
    def capabilities(self) -> TranslationCapabilities:
        return TranslationCapabilities(name="identity", local=True, uses_context=False)

    async def translate(
        self, text: str, src: str | None, dst: str, context: Sequence[str] = ()
    ) -> str:
        return text
