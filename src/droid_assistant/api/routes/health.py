"""Health, configuration, search, and presets."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ... import __version__
from ...backends import registry
from ...logging import current_levels, set_level
from ..schemas import ConfigPatchRequest, PresetRequest
from .deps import ServicesDep

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["system"])

Services = ServicesDep


@router.get("/health")
async def health(services: Services) -> dict[str, Any]:
    """Version, active backends, model status, GPU presence, disk (SRS §8.5).

    Deliberately cheap: `doctor` does the slow reachability probes. This is
    polled by the client before every recording (FR-UI-18) and must not become
    the thing that delays starting one.
    """
    settings = services.settings
    llm = services.llm()
    backends = registry.describe(
        settings,
        services.asr,
        services.diarization,
        registry.build_translation(settings, llm),
        llm,
    )
    models = services.model_status()
    return {
        "status": (
            "starting"
            if models["state"] == "loading"
            else "degraded"
            if services.validation.errors
            else "ok"
        ),
        "models": models,
        "version": __version__,
        "uptime_s": round(time.time() - services.started_at, 1),
        "backends": backends,
        "gpu": _gpu_info(),
        "disk": services.disk(),
        "local_only": settings.privacy.local_only,
        "active_sessions": services.sessions.active_ids,
        "plugins": [
            {"name": p["name"], "enabled": p["enabled"], "available": p["available"]}
            for p in services.plugins.listing()
        ],
        "errors": services.validation.errors,
        "warnings": services.validation.warnings,
    }


def _gpu_info() -> dict[str, Any]:
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
        return {"cuda_devices": count, "present": count > 0}
    except Exception:
        return {"cuda_devices": 0, "present": False}


@router.get("/config")
async def get_config(services: Services) -> dict[str, Any]:
    """Effective configuration, secrets redacted by construction (FR-CFG-4)."""
    return {
        "config": services.settings.redacted(),
        "log_levels": current_levels(),
        "mutable_at_runtime": sorted(ConfigPatchRequest.model_fields),
        "note": (
            "Backend changes take effect on the next session; a running session keeps the "
            "backend it started with (FR-CFG-8). Everything not listed in "
            "`mutable_at_runtime` needs a restart."
        ),
    }


@router.patch("/config")
async def patch_config(body: ConfigPatchRequest, services: Services) -> dict[str, Any]:
    """Hot-swap backends and policy for the *next* session (FR-CFG-8).

    Settings is frozen, so a change builds a new one and rebuilds the affected
    backends. Sessions already running keep the objects they hold, which is what
    makes this safe to do mid-recording.
    """
    settings = services.settings
    updates: dict[str, dict[str, Any]] = {}

    def section(name: str) -> dict[str, Any]:
        return updates.setdefault(name, settings.model_dump()[name])

    if body.asr_backend is not None:
        section("asr")["backend"] = body.asr_backend
    if body.asr_model is not None:
        section("asr")["model"] = body.asr_model
    if body.translation_backend is not None:
        section("translation")["backend"] = body.translation_backend
    if body.llm_model is not None:
        section("llm")["model"] = body.llm_model
    if body.llm_base_url is not None:
        section("llm")["base_url"] = body.llm_base_url
    if body.target_language is not None:
        section("capture")["target_language"] = body.target_language
    if body.default_mode is not None:
        section("capture")["default_mode"] = body.default_mode
    if body.local_only is not None:
        section("privacy")["local_only"] = body.local_only
    if body.session_cost_ceiling_usd is not None:
        section("privacy")["session_cost_ceiling_usd"] = body.session_cost_ceiling_usd
    if body.diarization_enabled is not None:
        section("diarization")["enabled"] = body.diarization_enabled

    if body.log_levels:
        for component, level in body.log_levels.items():
            set_level(component, level)

    if updates:
        from ...config import ConfigError, Settings

        try:
            new_settings = Settings(**{**settings.model_dump(), **updates})
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            await services.reconfigure(new_settings)
        except (ConfigError, registry.BackendError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "config": services.settings.redacted(),
        "log_levels": current_levels(),
        "applies_to": "the next session; running sessions are unaffected",
    }


@router.get("/search")
async def search(
    services: Services,
    q: str = Query(min_length=1),
    limit: int = Query(default=50, ge=1, le=200),
    session_id: str | None = None,
    include_artifacts: bool = True,
    mode: str = Query(default="fts", pattern="^(fts|semantic)$"),
) -> dict[str, Any]:
    """FR-SES-10: transcripts *and* artifacts, both highlighted."""
    if mode == "semantic":
        # FR-SES-11 is a v2 requirement. Saying so is better than silently
        # returning keyword results and looking broken.
        raise HTTPException(
            status_code=501,
            detail="semantic search ships in v2; embeddings are already being stored for it",
        )
    hits = await services.search.search(
        q, limit=limit, session_id=session_id, include_artifacts=include_artifacts
    )
    return {"query": q, "hits": [h.to_json() for h in hits], "count": len(hits)}


# --- presets (FR-SES-14) ----------------------------------------------------


@router.get("/presets")
async def list_presets(services: Services) -> dict[str, Any]:
    return {"presets": await services.repo.list_presets()}


@router.put("/presets")
async def upsert_preset(body: PresetRequest, services: Services) -> dict[str, Any]:
    return await services.repo.upsert_preset(body.name, body.config)


@router.delete("/presets/{preset_id}", status_code=204)
async def delete_preset(preset_id: str, services: Services) -> None:
    await services.repo.delete_preset(preset_id)


@router.get("/languages")
async def languages(services: Services) -> dict[str, Any]:
    """FR-CFG-3: the offered list comes from configuration, not from the client."""
    from ...backends.translation.llm import LANGUAGE_NAMES

    codes = services.settings.capture.languages
    return {
        "languages": [{"code": c, "name": LANGUAGE_NAMES.get(c, c)} for c in codes],
        "target_language": services.settings.capture.target_language,
        "multi_language_warning": (
            # R13, surfaced where the user chooses (FR-ASR-7's honest caveat).
            "Speech recognition detects one language at a time. If a sentence mixes "
            "languages, it will be transcribed in whichever one the model picks. Pinning a "
            "single language gives the best result."
            if len(codes) > 1
            else None
        ),
    }
