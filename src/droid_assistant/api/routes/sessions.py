"""Session routes: create, list, read, edit, stop, export, delete (SRS §5.2)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from ...domain import LatencyMode, SessionState
from ...events import EventType
from ...pipeline.modes import describe as describe_mode
from ...store import audio as audio_store
from .. import export as exporters
from ..schemas import (
    ChangeModeRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    EditUtteranceRequest,
    PluginRunRequest,
    RenameSpeakerRequest,
    UpdateSessionRequest,
)
from ..services import SessionError, SessionSpec
from .deps import ServicesDep

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["sessions"])

Services = ServicesDep


def _ws_urls(request: Request, session_id: str, token: str) -> tuple[str, str]:
    scheme = "wss" if request.url.scheme == "https" else "ws"
    base = f"{scheme}://{request.url.netloc}"
    return f"{base}/ws/ingest?token={token}", f"{base}/ws/sessions/{session_id}"


@router.post("/sessions", response_model=CreateSessionResponse, status_code=201)
async def create_session(
    body: CreateSessionRequest, request: Request, services: Services
) -> CreateSessionResponse:
    spec = SessionSpec(
        title=body.title,
        languages=body.languages,
        target_language=body.target_language,
        mode=body.mode,
        vocabulary=body.vocabulary,
        tags=body.tags,
        participants=body.participants,
        plugins=body.plugins,
        local_only=body.local_only,
        preset_id=body.preset_id,
        client_user_agent=request.headers.get("user-agent"),
        mic_label=body.mic_label,
        audio_constraints=body.audio_constraints,
        min_speakers=body.min_speakers,
        max_speakers=body.max_speakers,
    )
    try:
        record, token = await services.sessions.create(spec)
    except SessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    ingest_url, events_url = _ws_urls(request, record.id, token)
    return CreateSessionResponse(
        session_id=record.id,
        ingest_token=token,
        ws_url=ingest_url,
        events_url=events_url,
        session=record.to_json(),
    )


@router.get("/sessions")
async def list_sessions(
    services: Services,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    language: str | None = None,
    tag: str | None = None,
    since_ms: int | None = None,
    until_ms: int | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "language": language,
        "tag": tag,
        "since_ms": since_ms,
        "until_ms": until_ms,
        "query": q,
    }
    records = await services.repo.list_sessions(limit=limit, offset=offset, **filters)
    # The history list shows speaker count and which artifacts exist, because
    # those distinguish one conversation from another better than a weak
    # auto-generated title does (SRS §5.1).
    out = []
    for record in records:
        speakers = await services.repo.list_speakers(record.id)
        artifacts = await services.repo.list_artifacts(record.id, current_only=True)
        out.append(
            record.to_json(
                extra={
                    "speaker_count": len(speakers),
                    "artifact_kinds": sorted({a["kind"] for a in artifacts}),
                    "utterance_count": await services.db.fetch_value(
                        "SELECT count(*) FROM utterances WHERE session_id = ?",
                        (record.id,),
                        default=0,
                    ),
                    # FR-CAP-18's other half. A mark is worth nothing if the
                    # only way to find the recording holding it is to open every
                    # recording, so the count travels with the row.
                    "marked_count": await services.db.fetch_value(
                        "SELECT count(*) FROM utterances WHERE session_id = ? AND marked = 1",
                        (record.id,),
                        default=0,
                    ),
                    "live": record.id in services.sessions.active_ids,
                }
            )
        )
    # `total` counts what *matches*, not what exists: the client paginates
    # against it, and a total taken over a different set than the page makes
    # "Load more" stop early or never stop.
    total = await services.repo.count_sessions(**filters)
    return {
        "sessions": out,
        "total": total,
        "limit": limit,
        "offset": offset,
        # Filter options over *every* session, not just this page — otherwise a
        # tag used only on an old recording is unreachable until you have
        # scrolled far enough to load it.
        "facets": await services.repo.session_facets(),
        # Saves the client inferring the end from a short page — which is wrong
        # whenever the last page happens to be exactly full.
        "has_more": offset + len(out) < total,
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, services: Services) -> dict[str, Any]:
    record = await services.repo.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such session")
    utterances = await services.repo.list_utterances(session_id)
    speakers = await services.repo.list_speakers(session_id)
    artifacts = await services.repo.list_artifacts(session_id)
    return record.to_json(
        extra={
            "utterances": [u.to_event_data() | {"seq": u.seq} for u in utterances],
            "speakers": [
                {
                    "id": s.id,
                    "label": s.label,
                    "index": s.index,
                    "display_name": s.display_name,
                    "name": s.name,
                }
                for s in speakers
            ],
            "artifacts": artifacts,
            "artifacts_stale": await services.repo.artifacts_stale(session_id),
            "live": session_id in services.sessions.active_ids,
            "status": services.sessions.status(session_id),
        }
    )


@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str, body: UpdateSessionRequest, services: Services
) -> dict[str, Any]:
    record = await services.repo.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such session")
    fields = body.model_dump(exclude_none=True)
    if fields:
        await services.repo.update_session(session_id, **fields)
    updated = await services.repo.get_session(session_id)
    assert updated is not None
    return updated.to_json()


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str, services: Services) -> dict[str, Any]:
    record = await services.sessions.stop(session_id, reason="stopped by client")
    if record is None:
        raise HTTPException(status_code=404, detail="no such session")
    return record.to_json()


@router.post("/sessions/{session_id}/pause")
async def pause_session(session_id: str, services: Services) -> dict[str, Any]:
    active = services.sessions.get(session_id)
    if active is None:
        raise HTTPException(status_code=409, detail="this session is not running")
    await active.pipeline.pause()
    return {"paused": True}


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: str, services: Services) -> dict[str, Any]:
    active = services.sessions.get(session_id)
    if active is None:
        raise HTTPException(status_code=409, detail="this session is not running")
    await active.pipeline.resume()
    return {"paused": False}


@router.patch("/sessions/{session_id}/mode")
async def change_mode(
    session_id: str, body: ChangeModeRequest, services: Services
) -> dict[str, Any]:
    try:
        await services.sessions.set_mode(session_id, body.mode)
    except SessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # The switch lands at the next VAD boundary (FR-LAT-3), so this is accepted,
    # not applied — the client learns it happened from `session.mode_changed`.
    return {"requested_mode": str(body.mode), "applies": "at the next pause in speech"}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, services: Services) -> Response:
    record = await services.repo.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such session")
    if session_id in services.sessions.active_ids:
        await services.sessions.stop(session_id, reason="deleted")
    # FR-SES-12: purge the audio file too, not only the rows.
    if record.audio_path:
        path = Path(record.audio_path)
        if _within(path, services.settings.audio_dir):
            path.unlink(missing_ok=True)
    await services.repo.delete_session(session_id)
    services.bus.close_session(session_id)
    return Response(status_code=204)


def _within(path: Path, root: Path) -> bool:
    """NFR-SEC-7: never unlink outside the audio directory, whatever the row says."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


# --- utterances -------------------------------------------------------------


@router.patch("/utterances/{utterance_id}")
async def edit_utterance(
    utterance_id: str, body: EditUtteranceRequest, services: Services
) -> dict[str, Any]:
    existing = await services.repo.get_utterance(utterance_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="no such utterance")

    if body.marked is not None and body.marked != existing.marked:
        await services.repo.update_utterance(utterance_id, marked=body.marked)
        await services.bus.publish(
            existing.session_id,
            EventType.UTTERANCE_MARKED,
            {"utterance_id": utterance_id, "marked": body.marked},
        )
    if body.text is not None and body.text != existing.text:
        edited = await services.repo.edit_utterance_text(utterance_id, body.text)
        if edited is None:
            raise HTTPException(status_code=404, detail="no such utterance")
        utterance = edited
        await services.bus.publish(
            utterance.session_id,
            EventType.TRANSCRIPT_EDITED,
            {
                "utterance_id": utterance_id,
                "text": utterance.text,
                "text_original": utterance.text_original,
            },
        )
    else:
        refreshed = await services.repo.get_utterance(utterance_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="no such utterance")
        utterance = refreshed
    return utterance.to_event_data() | {
        "seq": utterance.seq,
        "session_id": utterance.session_id,
        "text_original": utterance.text_original,
        # FR-SES-9: the summary the session already holds is now stale.
        "artifacts_stale": await services.repo.artifacts_stale(utterance.session_id),
    }


# --- speakers ---------------------------------------------------------------


@router.get("/sessions/{session_id}/speakers")
async def list_speakers(session_id: str, services: Services) -> dict[str, Any]:
    speakers = await services.repo.list_speakers(session_id)
    return {
        "speakers": [
            {
                "id": s.id,
                "label": s.label,
                "index": s.index,
                "display_name": s.display_name,
                "name": s.name,
            }
            for s in speakers
        ]
    }


@router.patch("/speakers/{speaker_id}")
async def rename_speaker(
    speaker_id: str, body: RenameSpeakerRequest, services: Services
) -> dict[str, Any]:
    speaker = await services.repo.rename_speaker(speaker_id, body.display_name)
    if speaker is None:
        raise HTTPException(status_code=404, detail="no such speaker")
    await services.bus.publish(
        speaker.session_id,
        EventType.SPEAKER_CHANGED,
        {
            "speaker_id": speaker.id,
            "label": speaker.label,
            "display_name": speaker.display_name,
            "index": speaker.index,
        },
    )
    return {
        "id": speaker.id,
        "label": speaker.label,
        "index": speaker.index,
        "display_name": speaker.display_name,
        "name": speaker.name,
    }


# --- artifacts --------------------------------------------------------------


@router.get("/sessions/{session_id}/artifacts")
async def list_artifacts(
    session_id: str, services: Services, current_only: bool = False
) -> dict[str, Any]:
    return {
        "artifacts": await services.repo.list_artifacts(session_id, current_only=current_only),
        "stale": await services.repo.artifacts_stale(session_id),
    }


@router.post("/sessions/{session_id}/plugins/{name}/run")
async def run_plugin(
    session_id: str, name: str, services: Services, body: PluginRunRequest | None = None
) -> dict[str, Any]:
    """Run one plugin over this session, or over part of it.

    `utterance_ids` narrows it to particular lines — what "make an action item
    out of this message" posts. Omitting it means the whole conversation, which
    is what a re-run after an edit wants (FR-SES-9, FR-PLG-12).
    """
    if await services.repo.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="no such session")
    wanted = body.utterance_ids if body else None
    if wanted is not None:
        known = {u.id for u in await services.repo.list_utterances(session_id)}
        # Checked here rather than left to the plugin: an id from another
        # session would otherwise silently narrow the run to nothing, and an
        # empty artifact is indistinguishable from "there was nothing to find".
        if missing := [uid for uid in wanted if uid not in known]:
            raise HTTPException(
                status_code=404, detail=f"not utterances of this session: {', '.join(missing)}"
            )
    try:
        artifact, declined = await services.plugins.run_now(name, session_id, utterance_ids=wanted)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no plugin named {name!r}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if artifact is None:
        # A plugin that declined says why; one that simply found nothing does
        # not, and "produced no artifact" is the honest thing to report then.
        return {
            "ran": True,
            "artifact": None,
            "note": declined or "the plugin produced no artifact",
        }
    return {"ran": True, "artifact": artifact}


# --- audio ------------------------------------------------------------------


@router.get("/sessions/{session_id}/audio")
async def get_audio(session_id: str, request: Request, services: Services) -> Response:
    """Range-aware audio serving, so the player can seek (FR-UI-8)."""
    record = await services.repo.get_session(session_id)
    if record is None or not record.audio_path:
        raise HTTPException(status_code=404, detail="this session has no stored audio")
    path = Path(record.audio_path)
    if not _within(path, services.settings.audio_dir) or not path.exists():
        raise HTTPException(status_code=404, detail="the audio file for this session is missing")

    size = path.stat().st_size
    media_type = audio_store.MIME_TYPES.get(path.suffix, "application/octet-stream")
    headers = {"accept-ranges": "bytes", "cache-control": "private, max-age=3600"}

    requested = audio_store.parse_range(request.headers.get("range"), size)
    if requested is None:
        return StreamingResponse(
            audio_store.aiter_file(path, 0, size - 1),
            media_type=media_type,
            headers={**headers, "content-length": str(size)},
        )
    start, end = requested
    return StreamingResponse(
        audio_store.aiter_file(path, start, end),
        status_code=206,
        media_type=media_type,
        headers={
            **headers,
            "content-range": f"bytes {start}-{end}/{size}",
            "content-length": str(end - start + 1),
        },
    )


# --- export -----------------------------------------------------------------


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    services: Services,
    format: str = Query(default="md", pattern="^(md|json|srt|vtt|txt)$"),
    translated: bool = False,
) -> Response:
    record = await services.repo.get_session(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail="no such session")
    utterances = await services.repo.list_utterances(session_id)
    speakers = {s.id: s for s in await services.repo.list_speakers(session_id)}
    artifacts = await services.repo.list_artifacts(session_id)

    match format:
        case "md":
            body = exporters.to_markdown(record, utterances, speakers, artifacts)
        case "json":
            body = exporters.to_json(record, utterances, speakers, artifacts)
        case "srt":
            body = exporters.to_srt(utterances, speakers, translated=translated)
        case "vtt":
            body = exporters.to_vtt(utterances, speakers, translated=translated)
        case _:
            body = exporters.to_text(utterances, speakers)

    return Response(
        content=body,
        media_type=exporters.MEDIA_TYPES[format],
        headers={
            "content-disposition": f'attachment; filename="{exporters.filename(record, format)}"'
        },
    )


# --- modes ------------------------------------------------------------------


@router.get("/modes")
async def list_modes(services: Services) -> dict[str, Any]:
    """FR-LAT-7: each mode with its latency target and cloud implication."""
    cloud_asr = not services.asr.capabilities.local
    llm = services.llm()
    # Only true if text actually goes somewhere: a remote endpoint, permitted by
    # policy, that is reachable at all.
    cloud_llm = (
        services.settings.translation.enabled
        and not services.settings.llm.is_local
        and not services.settings.privacy.local_only
        and bool(getattr(llm, "available", True))
    )
    return {
        "modes": [
            describe_mode(mode, cloud_asr=cloud_asr, cloud_llm=cloud_llm) for mode in LatencyMode
        ],
        "default": str(services.settings.capture.default_mode),
        "streaming_backend": services.asr.capabilities.streaming,
    }


__all__ = ["SessionState", "router"]
