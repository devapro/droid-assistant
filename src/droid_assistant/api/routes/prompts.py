"""Saved prompts — the instructions an artifact can be generated with (FR-PLG-14).

A plugin's `config` is one setting with one value; these exist because the
interesting choice is per run and there is more than one right answer. "Summarise
this the way I summarise a customer call" and "…the way I summarise a standup"
are both correct, and neither belongs in a field the other overwrites.

CRUD over a table, and deliberately dull. What makes the feature worth having is
where the choice is offered — beside the button that spends the LLM call — not
anything happening here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ..schemas import PromptRequest
from .deps import ServicesDep

router = APIRouter(prefix="/api", tags=["prompts"])

Services = ServicesDep


@router.get("/prompts")
async def list_prompts(services: Services) -> dict[str, Any]:
    return {"prompts": await services.repo.list_prompts()}


@router.put("/prompts")
async def upsert_prompt(body: PromptRequest, services: Services) -> dict[str, Any]:
    """Create or rewrite one. `id` present is a rewrite, and covers renaming."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="a prompt needs a name")
    try:
        return await services.repo.upsert_prompt(name, body.instructions, body.id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no prompt with id {body.id!r}") from exc
    except Exception as exc:  # a name collision with a *different* prompt
        # The unique constraint is what stops two prompts sharing a name, and
        # "UNIQUE constraint failed: prompts.name" is not something to show
        # somebody who was typing in a text box.
        if "UNIQUE" not in str(exc):
            raise
        raise HTTPException(
            status_code=409, detail=f"another prompt is already called {name!r}"
        ) from exc


@router.delete("/prompts/{prompt_id}", status_code=204)
async def delete_prompt(prompt_id: str, services: Services) -> None:
    """Deleting a prompt leaves the artifacts it produced alone.

    Those are records of what was generated and what it said; removing the
    instruction does not make them untrue. Each keeps the prompt's *name* in its
    metadata rather than a reference, so the record survives the deletion.
    """
    await services.repo.delete_prompt(prompt_id)
