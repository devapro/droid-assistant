"""Plugin listing and configuration (FR-PLG-5, FR-PLG-6)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..schemas import PluginPatchRequest
from .deps import ServicesDep

router = APIRouter(prefix="/api", tags=["plugins"])

Services = ServicesDep


@router.get("/plugins")
async def list_plugins(services: Services, session_id: str | None = None) -> dict[str, Any]:
    """Each plugin with its declared config schema, so Settings renders a form
    from the schema rather than from hand-written markup (FR-PLG-5)."""
    return {
        "plugins": services.plugins.listing(session_id),
        "discovery_errors": services.plugins.discovery_errors,
        "directory": str(services.settings.plugins.directory),
        "timeout_s": services.settings.plugins.timeout_s,
        "trust_notice": (
            "Plugins run in-process with full server privileges. Installing one is "
            "equivalent to running arbitrary code on this machine."
        ),
    }


@router.patch("/plugins/{name}")
async def patch_plugin(name: str, body: PluginPatchRequest, services: Services) -> dict[str, Any]:
    plugin = services.plugins.plugins.get(name)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"no plugin named {name!r}")

    if body.config is not None:
        try:
            validated = services.plugins.validate_config(name, body.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        plugin.config = validated
        await services.repo.set_plugin_state(name, config=validated)

    if body.enabled is not None:
        services.plugins.set_enabled(name, body.enabled)
        await services.repo.set_plugin_state(name, enabled=body.enabled)

    return plugin.to_json(unavailable_reason=services.plugins.unavailable_reason(plugin))
