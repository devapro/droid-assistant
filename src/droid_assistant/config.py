"""Configuration: TOML file, overridden by environment variables (FR-CFG-1).

Validation happens once, at startup, and failure names the field, the value it
got, and what it will accept (FR-CFG-2). A configuration mistake should be a
readable sentence at boot, never a `KeyError` forty minutes into a meeting.

Precedence, highest first:

    1. explicit constructor arguments (tests)
    2. environment variables — ``DROID_ASR__MODEL=medium``
    3. ``$DROID_DATA/config.toml``
    4. field defaults

Secrets are never fields. Config holds the *name of the variable* to read
(``api_key_env``), so no key material can reach a log line, an API response, or
a config export (FR-CFG-4, NFR-SEC-5).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .domain import LatencyMode

Port = Annotated[int, Field(ge=1, le=65535)]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]


def default_data_dir() -> Path:
    """`$DROID_DATA_DIR`, else `./data` (SRS §5.9)."""
    return Path(os.environ.get("DROID_DATA_DIR", "./data")).expanduser()


class ServerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Loopback by default; LAN exposure must be a deliberate edit (NFR-SEC-1).
    host: str = "127.0.0.1"
    port: Port = 8000
    data_dir: Path = Field(default_factory=default_data_dir)
    # Empty ⇒ same-origin only. Entries are exact origins, checked on WS upgrade
    # whenever we are not bound to loopback (NFR-SEC-4).
    allowed_origins: list[str] = Field(default_factory=list)
    # A session whose client vanishes is finalised after this long (FR-SES-4).
    disconnect_grace_s: Annotated[float, Field(ge=1, le=3600)] = 90.0
    # Refuse to start a session below this much free disk (NFR-RES-7).
    min_free_disk_mb: Annotated[int, Field(ge=0)] = 2048
    warn_free_disk_mb: Annotated[int, Field(ge=0)] = 8192
    log_level: Literal["debug", "info", "warning", "error"] = "info"

    @model_validator(mode="after")
    def _warn_above_min(self) -> Self:
        if self.warn_free_disk_mb < self.min_free_disk_mb:
            raise ValueError(
                f"server.warn_free_disk_mb ({self.warn_free_disk_mb}) must be >= "
                f"server.min_free_disk_mb ({self.min_free_disk_mb}); otherwise the "
                "warning fires after sessions are already being refused"
            )
        return self


class CaptureConfig(BaseModel):
    """Defaults the client picks up, and the operator-editable language list."""

    model_config = {"extra": "forbid"}

    # FR-CFG-3: offered in the UI, editable here, never hard-coded in a component.
    languages: list[str] = Field(default_factory=lambda: ["en", "ru", "sr"])
    target_language: str = "en"
    default_mode: LatencyMode = LatencyMode.BALANCED
    chunk_ms: Annotated[int, Field(ge=20, le=2000)] = 200
    # Browser audio processing. Defaults are the SRS §8.4 recommendation for
    # multi-speaker capture and should be revisited against the R6 measurement.
    echo_cancellation: bool = False
    noise_suppression: bool = False
    auto_gain_control: bool = False
    # Client-side buffer cap before the oldest spilled audio is dropped (NFR-RES-5).
    client_buffer_cap_mb: Annotated[int, Field(ge=5, le=4096)] = 256

    @field_validator("languages")
    @classmethod
    def _non_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError(
                "capture.languages must list at least one BCP-47 code, e.g. ['en', 'ru']"
            )
        return v

    @model_validator(mode="after")
    def _target_is_known(self) -> Self:
        if self.target_language not in self.languages:
            raise ValueError(
                f"capture.target_language {self.target_language!r} is not in "
                f"capture.languages {self.languages}; add it so the UI can offer it"
            )
        return self


class VADConfig(BaseModel):
    model_config = {"extra": "forbid"}

    backend: Literal["silero", "energy"] = "silero"
    threshold: Ratio = 0.5
    min_speech_ms: Annotated[int, Field(ge=0)] = 250
    # A pause this long ends an utterance. Too short fragments sentences; too
    # long delays every Balanced-mode result by exactly this much.
    min_silence_ms: Annotated[int, Field(ge=0)] = 700
    speech_pad_ms: Annotated[int, Field(ge=0)] = 200
    # Hard ceiling so one monologue does not become one 40-minute utterance.
    max_speech_ms: Annotated[int, Field(ge=1000)] = 30_000


class DeepgramConfig(BaseModel):
    """Deepgram, reached over its own streaming WebSocket protocol."""

    model_config = {"extra": "forbid"}

    api_key_env: str = "DEEPGRAM_API_KEY"
    model: str = "nova-2"
    endpoint: str = "wss://api.deepgram.com/v1/listen"
    # Reporting only (FR-CFG-7). Check it against your own contract — list
    # pricing and negotiated pricing are rarely the same.
    price_per_minute_usd: Annotated[float, Field(ge=0)] = 0.0043


class OpenAIASRConfig(BaseModel):
    """OpenAI transcription — two endpoints behind one backend.

    * `model` is used for Balanced and Batch mode over `/v1/audio/transcriptions`.
    * `realtime_model` is used for Live mode over the Realtime WebSocket, which
      is the only one of the two that is genuinely streaming.

    Model choice is a real trade rather than a preference:

    | model | timestamps | speakers | notes |
    |---|---|---|---|
    | `gpt-4o-transcribe` | no | no | the general default |
    | `gpt-4o-mini-transcribe` | no | no | cheaper, weaker |
    | `gpt-4o-transcribe-diarize` | no | **yes** | speaker labels come from the model |
    | `whisper-1` | **yes** | no | the only one with word timings (FR-ASR-5) |
    """

    model_config = {"extra": "forbid"}

    api_key_env: str = "OPENAI_API_KEY"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-transcribe"
    realtime_model: str = "gpt-live-transcribe"
    # Left configurable because the transcription-session query string is not
    # pinned in the published API; pointing it elsewhere must not need a code
    # change.
    realtime_url: str = "wss://api.openai.com/v1/realtime?intent=transcription"
    # The Realtime API's own latency/quality dial, independent of our modes.
    realtime_delay: Literal["minimal", "low", "medium", "high", "xhigh"] = "low"
    # Steering text prepended to every request, on top of the per-session
    # vocabulary (FR-ASR-8).
    prompt: str = ""
    # Published list price per minute, per model, used only to *report* spend.
    # Note how much dearer the realtime model is than the batch one — that is a
    # real reason to prefer Balanced mode on a cloud backend.
    price_per_minute_usd: dict[str, float] = Field(
        default_factory=lambda: {
            "gpt-4o-transcribe": 0.006,
            "gpt-4o-mini-transcribe": 0.003,
            "gpt-transcribe": 0.0045,
            "gpt-4o-transcribe-diarize": 0.006,
            "whisper-1": 0.006,
            "gpt-live-transcribe": 0.017,
        }
    )
    # Used for a model absent from the table above, so an unknown model reports
    # *something* rather than silently costing nothing.
    fallback_price_per_minute_usd: Annotated[float, Field(ge=0)] = 0.006

    def price_for(self, model: str) -> float:
        return self.price_per_minute_usd.get(model, self.fallback_price_per_minute_usd)


#: Models whose response carries word-level timings (FR-ASR-5, FR-UI-8).
OPENAI_TIMESTAMP_MODELS = frozenset({"whisper-1"})
#: Models that return speaker labels of their own, which the pipeline prefers
#: over its own clustering when present.
OPENAI_DIARIZING_MODELS = frozenset({"gpt-4o-transcribe-diarize"})


class ASRConfig(BaseModel):
    model_config = {"extra": "forbid"}

    backend: str = "faster_whisper"
    model: str = "large-v3-turbo"
    device: Literal["auto", "cpu", "cuda", "metal"] = "auto"
    compute_type: str = "auto"
    beam_size: Annotated[int, Field(ge=1, le=10)] = 5
    # FR-ASR-9: above this no-speech probability the segment is dropped, which is
    # the second half of hallucination suppression after VAD gating.
    no_speech_threshold: Ratio = 0.6
    condition_on_previous_text: bool = False  # True amplifies hallucination loops
    word_timestamps: bool = True
    serbian_script: Literal["latin", "cyrillic"] = "latin"  # FR-ASR-10
    # Cloud providers, each with its own credential *variable name* — never a
    # credential (FR-CFG-4). Only the selected `backend` is ever used.
    deepgram: DeepgramConfig = Field(default_factory=DeepgramConfig)
    openai: OpenAIASRConfig = Field(default_factory=OpenAIASRConfig)


class DiarizationConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    backend: str = "sherpa"
    # None ⇒ infer the count (FR-DIA-2). Setting both to the same value pins it.
    min_speakers: Annotated[int, Field(ge=1)] | None = None
    max_speakers: Annotated[int, Field(ge=1)] | None = None
    clustering_threshold: Annotated[float, Field(gt=0)] = 0.5
    segmentation_model: str = "sherpa-onnx-pyannote-segmentation-3-0"
    embedding_model: str = "nemo_en_titanet_small"

    @model_validator(mode="after")
    def _range_ordered(self) -> Self:
        lo, hi = self.min_speakers, self.max_speakers
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(
                f"diarization.min_speakers ({lo}) must be <= diarization.max_speakers ({hi})"
            )
        return self


class TranslationConfig(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool = True
    backend: str = "llm"
    # FR-TRA-3. Below about 4 the pronoun and gender agreement this exists for
    # starts to fail on Slavic source text.
    context_utterances: Annotated[int, Field(ge=0, le=32)] = 6
    # FR-TRA-9: batch consecutive short utterances, but never past this delay.
    batch_max_utterances: Annotated[int, Field(ge=1, le=16)] = 4
    batch_max_chars: Annotated[int, Field(ge=1)] = 240
    batch_max_delay_ms: Annotated[int, Field(ge=0, le=5000)] = 600
    # Local CTranslate2 fallback (FR-TRA-6).
    local_model: str = "Helsinki-NLP/opus-mt-mul-en"
    local_model_dir: str = "opus-mt-mul-en-ct2"


class LLMConfig(BaseModel):
    """One OpenAI-shaped client serves cloud and local endpoints (SRS §6.2)."""

    model_config = {"extra": "forbid"}

    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1-mini"
    api_key_env: str = "OPENAI_API_KEY"
    max_tokens: Annotated[int, Field(ge=16)] = 2048
    temperature: Annotated[float, Field(ge=0, le=2)] = 0.2
    timeout_s: Annotated[float, Field(gt=0)] = 60.0
    max_retries: Annotated[int, Field(ge=0, le=10)] = 3
    # Used only to report spend; wrong numbers cost accuracy, not money (FR-CFG-7).
    price_per_1m_input_usd: Annotated[float, Field(ge=0)] = 0.40
    price_per_1m_output_usd: Annotated[float, Field(ge=0)] = 1.60

    @property
    def is_local(self) -> bool:
        host = self.base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        return host in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}


class PluginsConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # Extra directory scanned for drop-in plugin modules (FR-PLG-1).
    directory: Path = Path("./plugins")
    # None ⇒ every discovered plugin is enabled unless disabled in plugin_state.
    enabled: list[str] | None = None
    timeout_s: Annotated[float, Field(gt=0, le=3600)] = 180.0
    api_version: int = 1


class PrivacyConfig(BaseModel):
    model_config = {"extra": "forbid"}

    # FR-CFG-5. When true nothing outbound is attempted, by anything, ever.
    local_only: bool = False
    # FR-CFG-7. 0 ⇒ no ceiling. On exceeding it the session continues locally.
    session_cost_ceiling_usd: Annotated[float, Field(ge=0)] = 0.0


class AudioStorageConfig(BaseModel):
    model_config = {"extra": "forbid"}

    persist: bool = True  # FR-SIG-3
    codec: Literal["opus", "wav", "flac"] = "opus"
    bitrate_kbps: Annotated[int, Field(ge=8, le=256)] = 32  # ≈ 15 MB/h (NFR-RES-3)


class Settings(BaseSettings):
    """The effective configuration. Immutable once validated."""

    model_config = SettingsConfigDict(
        env_prefix="DROID_",
        env_nested_delimiter="__",
        extra="forbid",
        frozen=True,
        toml_file=None,  # supplied per-instance by `load()`
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    vad: VADConfig = Field(default_factory=VADConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    audio: AudioStorageConfig = Field(default_factory=AudioStorageConfig)

    # --- derived paths (SRS §5.9) -------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.server.data_dir / "droid.db"

    @property
    def audio_dir(self) -> Path:
        return self.server.data_dir / "audio"

    @property
    def models_dir(self) -> Path:
        return Path(os.environ.get("DROID_MODELS_DIR", self.server.data_dir / "models"))

    @property
    def config_path(self) -> Path:
        return self.server.data_dir / "config.toml"

    def ensure_dirs(self) -> None:
        for path in (self.server.data_dir, self.audio_dir, self.models_dir):
            path.mkdir(parents=True, exist_ok=True)

    def secret(self, env_var: str) -> str | None:
        """Read a credential at the point of use. Never stored, never logged."""
        value = os.environ.get(env_var)
        return value or None

    def redacted(self) -> dict[str, Any]:
        """Effective configuration for `GET /api/config` — no key material by
        construction, since only variable *names* are ever stored (FR-CFG-4)."""
        data = self.model_dump(mode="json")
        data["server"]["data_dir"] = str(self.server.data_dir)
        data["llm"]["credential_present"] = bool(self.secret(self.llm.api_key_env))
        for provider, config in (("deepgram", self.asr.deepgram), ("openai", self.asr.openai)):
            data["asr"][provider]["credential_present"] = bool(self.secret(config.api_key_env))
        return data

    # --- sources ------------------------------------------------------------

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings, env_settings, TomlConfigSettingsSource(settings_cls))


class ConfigError(Exception):
    """A configuration problem, already formatted for a human (FR-CFG-2)."""


def _format(error: ValidationError, source: Path | None) -> str:
    where = f" in {source}" if source else ""
    lines = [f"Invalid configuration{where}:"]
    for err in error.errors():
        field = ".".join(str(p) for p in err["loc"]) or "(root)"
        got = err.get("input")
        detail = err["msg"]
        lines.append(f"  {field}: {detail}" + (f" (got: {got!r})" if got is not None else ""))
    lines.append("\nSee docs/CONFIGURATION.md for every field and its accepted range.")
    return "\n".join(lines)


def load(path: Path | None = None, **overrides: Any) -> Settings:
    """Load and validate. Raises `ConfigError` with a readable message.

    `path` defaults to `$DROID_DATA/config.toml`, and a missing file is not an
    error — the defaults are a working configuration.
    """
    source = path or default_data_dir() / "config.toml"
    toml_file = source if source.is_file() else None

    class _Configured(Settings):
        model_config = SettingsConfigDict(**{**Settings.model_config, "toml_file": toml_file})

    try:
        return _Configured(**overrides)
    except ValidationError as exc:
        raise ConfigError(_format(exc, toml_file)) from exc
