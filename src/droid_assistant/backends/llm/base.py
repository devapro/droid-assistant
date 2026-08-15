"""The LLM interface (SRS §5.5, §5.6).

Plugins never construct a client and never see a credential (FR-PLG-8). They
receive one of these, already configured, already budget-aware, and already
respecting the local-only switch.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ...domain import Usage


class BudgetExceeded(RuntimeError):
    """The session's cost ceiling has been reached (FR-CFG-7).

    Callers catch this and fall back to local processing. The session continues
    and reports the switch; it does not fail.
    """


class LocalOnlyViolation(RuntimeError):
    """An outbound call was attempted while local-only is in force (FR-CFG-5)."""


@dataclass(slots=True, frozen=True)
class LLMCapabilities:
    name: str
    model: str
    local: bool
    streaming: bool = True


class LLMClient(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> LLMCapabilities: ...

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str: ...

    @abstractmethod
    def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Token stream. Implementations are async generators, so this is
        declared without `async`: an async generator function returns its
        iterator directly rather than a coroutine that yields one."""
        ...

    @property
    @abstractmethod
    def usage(self) -> Usage: ...

    async def probe(self) -> bool:
        """Is the endpoint reachable? Shown in Settings → Backends."""
        try:
            await self.complete("ping", max_tokens=1)
            return True
        except Exception:
            return False

    def mark_degraded(self) -> None:
        """Stop offering this client for the rest of the session.

        Called when the session's cost ceiling trips, so the fallback happens
        once rather than being rediscovered on every subsequent call.
        """

    async def close(self) -> None: ...
