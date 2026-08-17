"""Backend construction and startup validation.

One place decides which implementation a config string means, so adding a
backend is one entry here plus one module — and swapping one is a config change
with no code change (FR-ASR-1, FR-DIA-6, FR-CFG-8).

`validate()` runs before the server accepts a connection. It uses the declared
capabilities to reject configurations that cannot work — cloud ASR while
local-only is on, a single-language model offered speech it will confidently
mistranscribe — so the failure is a startup message rather than a broken session
(SRS §5.6).

It is deliberately narrow about what "cannot work" means. It used to refuse Live
mode to any backend declaring `streaming=False`, which was every local one; the
pipeline had meanwhile been running Live on exactly those backends by
re-decoding a window (`pipeline/localagreement.py`), so the check was rejecting
a configuration that works. Slow is not impossible, and the cost of getting that
distinction wrong is a mode nobody can reach.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..domain import LatencyMode
from .asr.base import ASRBackend
from .diarization.base import DiarizationBackend
from .llm.base import LLMClient
from .translation.base import TranslationBackend

log = logging.getLogger(__name__)


class BackendError(RuntimeError):
    """A backend cannot be built or configured, with the fix in the message."""


# --- ASR --------------------------------------------------------------------


def build_asr(settings: Settings, language: str | None = None) -> ASRBackend:
    """Build the ASR backend for a session.

    `language` selects a per-language override where one is configured
    (`asr.by_language`), because no single model is best at every language —
    Whisper is strongest across many, a specialised model wins on its own.
    """
    name, model = settings.asr.for_language(language)
    asr = (
        settings.asr
        if model == settings.asr.model
        else settings.asr.model_copy(update={"model": model})
    )
    match name:
        case "faster_whisper":
            from .asr.faster_whisper import FasterWhisperBackend

            return FasterWhisperBackend(asr, settings.models_dir)
        case "gigaam":
            from .asr.gigaam import GigaAMBackend

            return GigaAMBackend(settings.asr.gigaam, settings.models_dir)
        case "whisper_cpp":
            from .asr.whisper_cpp import WhisperCppBackend

            return WhisperCppBackend(asr, settings.models_dir)
        case "deepgram":
            from .asr.deepgram import DeepgramBackend

            return DeepgramBackend(settings.asr, settings.secret(settings.asr.deepgram.api_key_env))
        case "openai":
            from .asr.openai_asr import OpenAIASRBackend

            return OpenAIASRBackend(settings.asr, settings.secret(settings.asr.openai.api_key_env))
        case "mock":
            from .asr.mock import MockASRBackend

            return MockASRBackend()  # type: ignore[return-value]
        case _:
            raise BackendError(
                f"unknown asr.backend {name!r}. Available: faster_whisper, whisper_cpp, "
                "gigaam, deepgram, openai, mock."
            )


# --- diarization ------------------------------------------------------------


def build_diarization(settings: Settings) -> DiarizationBackend | None:
    if not settings.diarization.enabled:
        return None
    name = settings.diarization.backend
    match name:
        case "sherpa":
            from .diarization.sherpa import SherpaDiarizationBackend

            return SherpaDiarizationBackend(settings.diarization, settings.models_dir)
        case "pyannote":
            from .diarization.pyannote import PyannoteDiarizationBackend

            return PyannoteDiarizationBackend(settings.diarization)
        case "mock":
            from .diarization.mock import MockDiarizationBackend

            return MockDiarizationBackend()  # type: ignore[return-value]
        case _:
            raise BackendError(
                f"unknown diarization.backend {name!r}. Available: sherpa, pyannote, mock. "
                "Set diarization.enabled = false to run without speaker attribution."
            )


# --- LLM and translation ----------------------------------------------------


def build_llm(
    settings: Settings,
    *,
    local_only: bool = False,
    budget_usd: float = 0.0,
    on_usage: Callable[..., Any] | None = None,
) -> LLMClient:
    from .llm.openai_compat import NullLLMClient, OpenAICompatClient

    effective_local_only = local_only or settings.privacy.local_only
    key = settings.secret(settings.llm.api_key_env)
    if effective_local_only and not settings.llm.is_local:
        return NullLLMClient(
            "local-only is enabled and the configured LLM endpoint is remote; "
            "LLM-dependent features are unavailable for this session"
        )
    if not key and not settings.llm.is_local:
        return NullLLMClient(
            f"no LLM credential in ${settings.llm.api_key_env}; set it in .env, or point "
            "llm.base_url at a local endpoint such as Ollama"
        )
    return OpenAICompatClient(
        settings.llm,
        key,
        local_only=effective_local_only,
        budget_usd=budget_usd or settings.privacy.session_cost_ceiling_usd,
        on_usage=on_usage,
    )


def build_translation(settings: Settings, llm: LLMClient) -> TranslationBackend | None:
    if not settings.translation.enabled:
        return None
    name = settings.translation.backend
    match name:
        case "llm":
            from .translation.llm import LLMTranslationBackend

            return LLMTranslationBackend(llm, settings.translation.context_utterances)
        case "ctranslate2":
            from .translation.ctranslate2 import CTranslate2Backend

            return CTranslate2Backend(settings.translation, settings.models_dir)
        case "identity":
            from .translation.ctranslate2 import IdentityTranslationBackend

            return IdentityTranslationBackend()
        case _:
            raise BackendError(
                f"unknown translation.backend {name!r}. Available: llm, ctranslate2, identity."
            )


def build_translation_fallback(settings: Settings) -> TranslationBackend | None:
    """The local path used when the cloud one is refused or exhausted (NFR-REL-4,
    FR-CFG-7). None when the primary backend is already local."""
    if settings.translation.backend == "ctranslate2":
        return None
    from .translation.ctranslate2 import CTranslate2Backend

    return CTranslate2Backend(settings.translation, settings.models_dir)


# --- validation -------------------------------------------------------------


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_failed(self) -> None:
        if self.errors:
            raise BackendError(
                "This configuration cannot work:\n"
                + "\n".join(f"  • {e}" for e in self.errors)
                + "\n\nFix these in config.toml, or run `droid-assistant doctor` for detail."
            )


def validate(
    settings: Settings, asr: ASRBackend, requested_mode: LatencyMode | None = None
) -> ValidationReport:
    """Check the configuration against what the backends say they can do."""
    report = ValidationReport()
    caps = asr.capabilities
    mode = requested_mode or settings.capture.default_mode

    if mode is LatencyMode.LIVE:
        # Live has never needed a streaming backend, whatever this check used to
        # say. The pipeline drives it by re-decoding a sliding window through
        # `transcribe()` and committing what two hypotheses agree on
        # (LocalAgreement-2) — the same call every other mode makes, so every
        # backend can serve it. What Live needs is headroom, not an API.
        #
        # Imported here rather than at module scope: `pipeline` imports this
        # package, so the cycle only stays broken while this stays local.
        from ..pipeline.modes import LIVE

        window_s = LIVE.window_max_ms // 1000
        if caps.local:
            report.warnings.append(
                f"Live mode re-decodes a window of up to {window_s} s every "
                f"{LIVE.window_step_ms} ms, on top of the final decode of every segment. "
                f"Check that {caps.name} keeps up on this machine (SRS R2): recognition that "
                "falls behind does not catch up, and Balanced is the mode that scales down."
            )
        else:
            report.warnings.append(
                f"Live mode re-decodes a window of up to {window_s} s every "
                f"{LIVE.window_step_ms} ms, which against {caps.name} is a request that often — "
                "the audio billed for a session exceeds its length, and the estimate in the UI "
                "counts it honestly."
            )

    if not caps.local:
        if settings.privacy.local_only:
            report.errors.append(
                f"privacy.local_only is on but asr.backend ({caps.name}) sends audio off this "
                "machine. Set asr.backend = 'faster_whisper', or turn local-only off."
            )
        else:
            report.warnings.append(
                f"asr.backend ({caps.name}) is a cloud backend: raw audio leaves this server "
                "for every session (C-4). Sessions are flagged accordingly in the UI."
            )

    languages = settings.capture.languages
    if caps.languages is not None and len(caps.languages) == 1:
        # A single-language model given other speech does not fail — it returns
        # confident nonsense, which is far worse than an error at startup.
        only = next(iter(caps.languages))
        unsupported = [c for c in languages if c.split("-")[0] != only]
        if unsupported and not settings.asr.by_language:
            report.errors.append(
                f"{caps.name} only recognises {only!r}, but {unsupported} are offered and no "
                "per-language routing is configured. Add for example:\n"
                f"      [asr.by_language.{only}]\n"
                f'      backend = "{settings.asr.backend}"\n'
                "  and leave asr.backend as a model that covers the rest."
            )
    else:
        for code in languages:
            if not caps.supports_language(code):
                report.warnings.append(
                    f"{caps.name} does not list {code!r} among its languages; sessions pinned "
                    "to it may transcribe poorly."
                )

    for code in settings.asr.by_language:
        if code not in languages:
            report.warnings.append(
                f"asr.by_language has an entry for {code!r}, which is not in capture.languages "
                f"{languages}, so it will never be used."
            )

    if len(settings.capture.languages) > 1:
        # R13, stated once at startup rather than discovered mid-meeting.
        report.warnings.append(
            "several languages are offered: Whisper detects one language per window, so "
            "sentences mixing languages will be transcribed in whichever one wins. Pin a single "
            "language per session where you can."
        )

    if settings.translation.enabled and settings.translation.backend == "llm":
        if settings.privacy.local_only and not settings.llm.is_local:
            report.errors.append(
                "translation.backend = 'llm' with a remote llm.base_url, while "
                "privacy.local_only is on. Point llm.base_url at a local endpoint, or set "
                "translation.backend = 'ctranslate2'."
            )
        elif not settings.secret(settings.llm.api_key_env) and not settings.llm.is_local:
            report.warnings.append(
                f"no LLM credential in ${settings.llm.api_key_env}: translation and the summary "
                "and action-item plugins will be unavailable until one is set."
            )

    if settings.diarization.enabled and settings.capture.default_mode is LatencyMode.LIVE:
        report.warnings.append(
            "in Live mode partial hypotheses carry no speaker label, by design — diarization "
            "needs a completed segment (FR-DIA-10)."
        )

    return report


def describe(
    settings: Settings,
    asr: ASRBackend,
    diarization: DiarizationBackend | None,
    translation: TranslationBackend | None,
    llm: LLMClient,
) -> dict[str, Any]:
    """Backend summary for `GET /api/health` and Settings → Backends."""
    return {
        "asr": {
            "backend": settings.asr.backend,
            "name": asr.capabilities.name,
            "streaming": asr.capabilities.streaming,
            "local": asr.capabilities.local,
            "word_timestamps": asr.capabilities.word_timestamps,
        },
        "diarization": (
            {
                "backend": settings.diarization.backend,
                "name": diarization.capabilities.name,
                "local": diarization.capabilities.local,
            }
            if diarization
            else {"backend": None, "enabled": False}
        ),
        "translation": (
            {
                "backend": settings.translation.backend,
                "name": translation.capabilities.name,
                "local": translation.capabilities.local,
                "uses_context": translation.capabilities.uses_context,
                # An LLM-backed translator is only as available as its LLM.
                "available": (
                    bool(getattr(llm, "available", True))
                    if settings.translation.backend == "llm"
                    else True
                ),
            }
            if translation
            else {"backend": None, "enabled": False, "available": False}
        ),
        "llm": {
            "model": llm.capabilities.model,
            "local": llm.capabilities.local,
            "available": getattr(llm, "available", True),
        },
    }
