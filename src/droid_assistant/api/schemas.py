"""Request and response models for the HTTP API (SRS §5.2)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..domain import LatencyMode


class CreateSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = None
    languages: list[str] = Field(default_factory=list)
    target_language: str | None = None
    mode: LatencyMode | None = None
    vocabulary: list[str] = Field(default_factory=list, max_length=200)
    tags: list[str] = Field(default_factory=list)
    participants: list[str] = Field(default_factory=list)
    #: None ⇒ every enabled plugin. An explicit list narrows it for this session.
    plugins: list[str] | None = None
    local_only: bool = False
    preset_id: str | None = None
    mic_label: str | None = None
    audio_constraints: dict[str, Any] = Field(default_factory=dict)
    min_speakers: Annotated[int, Field(ge=1, le=20)] | None = None
    max_speakers: Annotated[int, Field(ge=1, le=20)] | None = None


class CreateSessionResponse(BaseModel):
    session_id: str
    ingest_token: str
    ws_url: str
    events_url: str
    session: dict[str, Any]


class UpdateSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    title: str | None = None
    tags: list[str] | None = None
    participants: list[str] | None = None
    source_languages: list[str] | None = None
    target_language: str | None = None
    vocabulary: list[str] | None = None


class ChangeModeRequest(BaseModel):
    mode: LatencyMode


class EditUtteranceRequest(BaseModel):
    model_config = {"extra": "forbid"}

    text: str | None = None
    marked: bool | None = None


class RenameSpeakerRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)


class PluginPatchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool | None = None
    config: dict[str, Any] | None = None


class PluginRunRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: The lines to run over. `null` — the ordinary case — means the whole
    #: session. An empty list is rejected rather than read as "everything":
    #: a UI that meant to send one id and sent none would otherwise summarise
    #: the entire conversation and look like it had worked.
    utterance_ids: Annotated[list[str], Field(min_length=1)] | None = None


class PresetRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=80)
    config: dict[str, Any]


class ModelDownloadRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: `backend:model`, or a bare model name meaning the configured backend.
    id: str = Field(min_length=1, max_length=200)


class ConfigPatchRequest(BaseModel):
    """Runtime-mutable configuration only.

    Everything here takes effect on the *next* session (FR-CFG-8) except log
    levels, which apply immediately (NFR-MNT-3). Fields not listed require a
    restart, and the UI says so rather than appearing to apply.
    """

    model_config = {"extra": "forbid"}

    asr_backend: str | None = None
    asr_model: str | None = None
    #: Per-language routing, as `{"ru": "gigaam:v3-rnnt"}` (FR-ASR-7). Merged
    #: into the existing map rather than replacing it, so a UI editing one row
    #: does not have to send the others back; `""` removes an entry, which is
    #: how "use the default for this language" is expressed.
    asr_by_language: dict[str, str] | None = None
    translation_backend: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    target_language: str | None = None
    default_mode: LatencyMode | None = None
    local_only: bool | None = None
    session_cost_ceiling_usd: Annotated[float, Field(ge=0)] | None = None
    diarization_enabled: bool | None = None
    log_levels: dict[str, str] | None = None


class ErrorResponse(BaseModel):
    """Errors name the component and say what to do about it (FR-UI-9)."""

    error: str
    component: str = "server"
    remedy: str | None = None


ExportFormat = Literal["md", "json", "srt", "vtt", "txt"]
