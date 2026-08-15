"""Pluggable backends. Each subpackage defines an interface in `base.py`;
siblings implement it. NFR-MNT-1 requires at least two implementations each,
which is what keeps the abstractions honest rather than decorative."""

from .registry import (
    BackendError,
    ValidationReport,
    build_asr,
    build_diarization,
    build_llm,
    build_translation,
    describe,
    validate,
)

__all__ = [
    "BackendError",
    "ValidationReport",
    "build_asr",
    "build_diarization",
    "build_llm",
    "build_translation",
    "describe",
    "validate",
]
