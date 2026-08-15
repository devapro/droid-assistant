"""One OpenAI-shaped client for both the cloud and a local endpoint (SRS §6.2).

Azure OpenAI, OpenRouter, Ollama, vLLM, LM Studio, and llama.cpp all speak this
request shape, so C-3 — "works with no internet" — costs a base URL rather than
a second implementation. NFR-MNT-1 counts the cloud and local endpoints as the
two implementations that keep the abstraction honest, and they differ in the
ways that matter: context length, streaming behaviour, and reliability.

The client is constructed per session so that cost accounting, the spend ceiling
(FR-CFG-7), and the per-session local-only override (FR-CFG-6) are all scoped to
one recording.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from typing import Any

from ...config import LLMConfig
from ...domain import Usage
from .base import BudgetExceeded, LLMCapabilities, LLMClient, LocalOnlyViolation

log = logging.getLogger(__name__)


class OpenAICompatClient(LLMClient):
    def __init__(
        self,
        config: LLMConfig,
        api_key: str | None,
        *,
        local_only: bool = False,
        budget_usd: float = 0.0,
        on_usage: Any = None,  # async (Usage, provider: str) -> None
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._local_only = local_only
        self._budget_usd = budget_usd
        self._on_usage = on_usage
        self._usage = Usage()
        self._client: Any = None
        self._degraded = False

    # --- policy -------------------------------------------------------------

    @property
    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(
            name="openai-compat", model=self._config.model, local=self._config.is_local
        )

    @property
    def usage(self) -> Usage:
        return self._usage

    @property
    def available(self) -> bool:
        """False when policy forbids using it — the UI shows dependent plugins
        as unavailable rather than letting them fail one by one (FR-CFG-5)."""
        if self._local_only and not self._config.is_local:
            return False
        if self._degraded:
            return False
        return bool(self._api_key) or self._config.is_local

    def _guard(self) -> None:
        if self._degraded:
            raise BudgetExceeded(
                "this session has been switched to local processing after reaching its cost ceiling"
            )
        if self._local_only and not self._config.is_local:
            raise LocalOnlyViolation(
                f"local-only is enabled and llm.base_url ({self._config.base_url}) is remote. "
                "Point it at a local endpoint (Ollama, vLLM, llama.cpp) or turn local-only off."
            )
        if self._budget_usd and self._usage.cost_usd >= self._budget_usd:
            raise BudgetExceeded(
                f"session spend ${self._usage.cost_usd:.4f} reached the ceiling "
                f"${self._budget_usd:.2f}; continuing locally"
            )

    def _ensure(self) -> Any:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError("the LLM client needs: uv sync --extra cloud") from exc
            self._client = AsyncOpenAI(
                api_key=self._api_key or "not-needed",  # local servers ignore it
                base_url=self._config.base_url,
                timeout=self._config.timeout_s,
                max_retries=0,  # retried here, with our own backoff (NFR-REL-4)
            )
        return self._client

    # --- accounting ---------------------------------------------------------

    def _price(self, prompt_tokens: int, completion_tokens: int) -> float:
        if self._config.is_local:
            return 0.0
        return (
            float(
                prompt_tokens * self._config.price_per_1m_input_usd
                + completion_tokens * self._config.price_per_1m_output_usd
            )
            / 1_000_000
        )

    async def _record(self, prompt_tokens: int, completion_tokens: int) -> None:
        usage = Usage(
            prompt_tokens, completion_tokens, self._price(prompt_tokens, completion_tokens)
        )
        self._usage = self._usage + usage
        if self._on_usage is not None:
            await self._on_usage(usage, self._config.model)

    # --- calls --------------------------------------------------------------

    def _messages(self, prompt: str, system: str | None) -> list[dict[str, str]]:
        messages = [{"role": "user", "content": prompt}]
        if system:
            messages.insert(0, {"role": "system", "content": system})
        return messages

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        self._guard()
        client = self._ensure()
        last: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = await client.chat.completions.create(
                    model=self._config.model,
                    messages=self._messages(prompt, system),
                    max_tokens=max_tokens or self._config.max_tokens,
                    temperature=(self._config.temperature if temperature is None else temperature),
                )
                usage = getattr(response, "usage", None)
                await self._record(
                    getattr(usage, "prompt_tokens", 0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0,
                )
                return (response.choices[0].message.content or "").strip()
            except Exception as exc:
                last = exc
                if not _retryable(exc) or attempt == self._config.max_retries:
                    break
                await asyncio.sleep(_backoff(attempt))
        assert last is not None
        log.warning(
            "LLM call failed", extra={"model": self._config.model, "error": str(last)[:200]}
        )
        raise last

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        self._guard()
        client = self._ensure()
        stream = await client.chat.completions.create(
            model=self._config.model,
            messages=self._messages(prompt, system),
            max_tokens=max_tokens or self._config.max_tokens,
            temperature=self._config.temperature if temperature is None else temperature,
            stream=True,
            stream_options={"include_usage": True},
        )
        prompt_tokens = completion_tokens = 0
        async for chunk in stream:
            if chunk.usage is not None:
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0
            for choice in chunk.choices:
                if choice.delta and choice.delta.content:
                    yield choice.delta.content
        await self._record(prompt_tokens, completion_tokens)

    def mark_degraded(self) -> None:
        """Stop offering this client for the rest of the session — used after the
        budget ceiling trips, so the fallback happens once rather than per call."""
        self._degraded = True

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


def _retryable(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"}:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status == 429 or status >= 500)


def _backoff(attempt: int) -> float:
    """Exponential with jitter (NFR-REL-4). Jitter matters because a session
    translating per utterance retries in bursts."""
    return float(min(20.0, 2**attempt) * (0.5 + random.random() / 2))


class NullLLMClient(LLMClient):
    """Stands in when no LLM is configured or policy forbids one.

    Plugins depending on it report as unavailable instead of raising, which is
    what FR-CFG-5 asks for.
    """

    def __init__(self, reason: str = "no LLM configured") -> None:
        self.reason = reason

    @property
    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(name="none", model="", local=True, streaming=False)

    @property
    def usage(self) -> Usage:
        return Usage()

    @property
    def available(self) -> bool:
        return False

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        raise RuntimeError(self.reason)

    def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        raise RuntimeError(self.reason)

    async def probe(self) -> bool:
        return False
