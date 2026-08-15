"""LLM clients. One OpenAI-shaped interface serves cloud and local endpoints."""

from .base import BudgetExceeded, LLMCapabilities, LLMClient, LocalOnlyViolation

__all__ = ["BudgetExceeded", "LLMCapabilities", "LLMClient", "LocalOnlyViolation"]
