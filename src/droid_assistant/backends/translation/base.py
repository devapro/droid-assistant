"""The translation backend interface (SRS §5.6).

`context` is not optional decoration. Sentence-isolated MT destroys pronoun
reference, gender agreement, and register — which is exactly what goes wrong
translating Russian and Serbian conversation into English, where the referent
that fixes a gender is two utterances back (FR-TRA-3, SRS §6.2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class TranslationCapabilities:
    name: str
    local: bool
    uses_context: bool
    languages: frozenset[str] | None = None


class TranslationBackend(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> TranslationCapabilities: ...

    @abstractmethod
    async def translate(
        self, text: str, src: str | None, dst: str, context: Sequence[str] = ()
    ) -> str: ...

    async def translate_batch(
        self, texts: Sequence[str], src: str | None, dst: str, context: Sequence[str] = ()
    ) -> list[str]:
        """FR-TRA-9: several short utterances in one request.

        The default is sequential; a backend that can do better overrides it.
        """
        return [await self.translate(text, src, dst, context) for text in texts]

    async def load(self) -> None: ...

    async def close(self) -> None: ...

    @property
    def name(self) -> str:
        return self.capabilities.name


def same_language(src: str | None, dst: str) -> bool:
    """FR-TRA-5: an English utterance in an EN-target session costs nothing."""
    if not src:
        return False
    return src.split("-")[0].lower() == dst.split("-")[0].lower()
