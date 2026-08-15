"""Speech models: what exists, what is on disk, and fetching the rest.

Model choice is language-dependent, and until this existed it could only be
changed by editing `config.toml` and restarting — which is not a thing anyone
does between two recordings. These endpoints are what makes Settings → Speech
recognition a real control rather than a read-out.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from ...backends.asr import catalog
from ..schemas import ModelDownloadRequest
from .deps import ServicesDep

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/models", tags=["models"])

Services = ServicesDep


@router.get("")
async def list_models(services: Services) -> dict[str, Any]:
    """Every offerable model with its state, plus the routing in force.

    `routing` is keyed by the languages the operator has actually configured
    (`capture.languages`), because that — not the catalogue — is the set of
    decisions in front of them.
    """
    settings = services.settings
    rows = catalog.inventory(settings)
    listed = {str(row["id"]) for row in rows}
    # A download for something outside the catalogue — a custom fine-tune, say —
    # would otherwise vanish from this list the moment it failed, leaving the
    # operator with a button that did nothing and no way to see why.
    for model_id in services.tracked_downloads():
        if model_id not in listed:
            backend, model = catalog.parse(model_id, settings.asr.backend)
            rows.append(catalog.row(catalog.spec_for(backend, model), settings))

    for row in rows:
        # A download this server started is neither present nor absent yet.
        row["state"] = services.download_state(str(row["id"])) or row["state"]
        row["error"] = services.download_error(str(row["id"]))

    default_id = f"{settings.asr.backend}:{settings.asr.model}"
    return {
        "models": rows,
        "default": default_id,
        "routing": {
            code: {
                "id": f"{b}:{m}",
                # False where the language falls back to the default, which is
                # what lets the UI show "use default" rather than a duplicate.
                "explicit": code in settings.asr.by_language,
            }
            for code in settings.capture.languages
            for b, m in [settings.asr.for_language(code)]
        },
        "languages": settings.capture.languages,
        "models_dir": str(settings.models_dir),
        "note": (
            "A model change applies to the next session; a recording in progress keeps the "
            "model it started with (FR-CFG-8)."
        ),
    }


@router.post("/download", status_code=202)
async def download_model(
    body: ModelDownloadRequest, services: Services, response: Response
) -> dict[str, Any]:
    """Fetch a model in the background.

    202, not 200: `large-v3` is three gigabytes, and holding a request open for
    the length of that is how a proxy times out mid-download. Poll `GET
    /api/models` for the state.
    """
    backend, model = catalog.parse(body.id, services.settings.asr.backend)
    spec = catalog.spec_for(backend, model)
    if not spec.local:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{spec.id} is a cloud backend — there are no weights to download. "
                f"Set its credential in the environment instead."
            ),
        )
    state, _size = catalog.state(spec, services.settings)
    if state == "present":
        response.status_code = 200  # nothing was accepted for later; it is done
        return {"id": spec.id, "state": "present"}

    services.start_model_download(spec)
    return {"id": spec.id, "state": "downloading"}
