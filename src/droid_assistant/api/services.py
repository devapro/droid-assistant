"""Application services: the object graph the routes depend on.

One container built at startup and shared, rather than module-level globals, so
tests construct a whole application against a temporary directory and mock
backends in three lines.

`SessionManager` owns session lifecycle — creation, the running pipeline, the
disconnect grace period, and finalisation — because that logic has to be
identical whether it is reached from the API, from a WebSocket closing, or from
a crash recovery sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..backends import registry
from ..backends.asr import catalog
from ..backends.asr.base import ASRBackend
from ..backends.asr.catalog import ModelSpec
from ..backends.diarization.base import DiarizationBackend
from ..backends.llm.base import LLMClient
from ..backends.translation.base import TranslationBackend
from ..config import Settings
from ..domain import (
    Artifact,
    LatencyMode,
    SessionState,
    Speaker,
    Usage,
    Utterance,
    new_id,
    now_ms,
)
from ..events import EventBus, EventType
from ..pipeline.orchestrator import SessionPipeline
from ..plugins.api import format_transcript
from ..plugins.host import PluginHost, discover
from ..store import Database, Repository, SearchIndex, SessionAudioWriter
from ..store.db import dumps
from ..store.repository import SessionRecord

log = logging.getLogger(__name__)

#: How long session creation waits for a model that is still loading. Long
#: enough for a local model already on disk, short enough that a first-run
#: download is reported rather than silently hung on.
MODEL_WAIT_S = 8.0

#: An ingest token outlives any plausible session, so a reconnect after a long
#: outage still authenticates (FR-CAP-6). It is revoked on stop regardless.
TOKEN_TTL_MS = 12 * 60 * 60 * 1000


class SessionError(Exception):
    """A session cannot be started or changed, with the reason for the user."""


@dataclass(slots=True)
class SessionSpec:
    """What the client asks for when creating a session (SRS §5.2)."""

    title: str | None = None
    languages: list[str] = field(default_factory=list)
    target_language: str | None = None
    mode: LatencyMode | None = None
    vocabulary: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    plugins: list[str] | None = None
    local_only: bool = False
    preset_id: str | None = None
    client_user_agent: str | None = None
    mic_label: str | None = None
    audio_constraints: dict[str, Any] = field(default_factory=dict)
    min_speakers: int | None = None
    max_speakers: int | None = None


class RepositoryStore:
    """The read-only `PluginStore` a plugin receives (SRS §5.5)."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    async def transcript(
        self, session_id: str, *, include_speakers: bool = True, include_translation: bool = False
    ) -> str:
        utterances = await self._repo.list_utterances(session_id)
        speakers = {s.id: s for s in await self._repo.list_speakers(session_id)}
        return format_transcript(
            utterances,
            speakers,
            include_speakers=include_speakers,
            include_translation=include_translation,
        )

    async def utterances(self, session_id: str) -> list[Utterance]:
        return await self._repo.list_utterances(session_id)

    async def speakers(self, session_id: str) -> list[Speaker]:
        return await self._repo.list_speakers(session_id)

    async def session_metadata(self, session_id: str) -> dict[str, Any]:
        record = await self._repo.get_session(session_id)
        return record.to_json() if record else {}


@dataclass
class ActiveSession:
    record: SessionRecord
    pipeline: SessionPipeline
    llm: LLMClient
    #: Set when the recording client disconnects; cancelled if it comes back
    #: within the grace period (FR-SES-4).
    grace_task: asyncio.Task[None] | None = None
    ingest_connected: bool = False
    #: Sequence tracking belongs to the *session*, not the socket. A reconnect
    #: opens a new socket and retransmits from the first gap (FR-CAP-6); if the
    #: counter reset with the socket, those chunks would sit forever waiting for
    #: sequence numbers that were already delivered.
    ingest_state: Any = None
    #: Set once, so the switch to local processing is announced once rather
    #: than on every subsequent charge.
    ceiling_hit: bool = False
    started_monotonic: float = field(default_factory=time.monotonic)


class SessionManager:
    def __init__(self, services: Services) -> None:
        self._services = services
        self._sessions: dict[str, ActiveSession] = {}
        self._lock = asyncio.Lock()

    # --- queries ------------------------------------------------------------

    def get(self, session_id: str) -> ActiveSession | None:
        return self._sessions.get(session_id)

    @property
    def active_ids(self) -> list[str]:
        return list(self._sessions)

    def status(self, session_id: str) -> dict[str, Any] | None:
        active = self._sessions.get(session_id)
        return active.pipeline.status() if active else None

    # --- creation -----------------------------------------------------------

    async def create(self, spec: SessionSpec) -> tuple[SessionRecord, str]:
        services = self._services
        settings = services.settings

        self._check_disk()
        # An already-downloaded model loads in a second or two, so wait briefly
        # rather than refusing a recording that would have been fine. Only a
        # genuine download — minutes, not seconds — gets turned away.
        if services.model_state == "loading":
            await services.wait_for_models(timeout=MODEL_WAIT_S)
        if services.model_state == "loading":
            raise SessionError(
                "The speech model is still loading. On a first run this downloads several "
                "gigabytes; the recording would have nothing to transcribe with. Try again "
                "in a few minutes — progress is in the server log."
            )
        if services.model_state == "failed":
            raise SessionError(f"The speech model could not be loaded: {services.model_error}")

        mode = spec.mode or settings.capture.default_mode
        # A session pinned to one language may route to a different engine
        # (asr.by_language) — a Russian-specialised model, say, while everything
        # else stays on Whisper.
        languages = spec.languages or list(settings.capture.languages)
        pinned = languages[0] if len(languages) == 1 else None
        asr = await services.asr_for(pinned)

        report = registry.validate(settings, asr, mode)
        if not report.ok:
            raise SessionError("; ".join(report.errors))

        record = SessionRecord(
            id=new_id("sess"),
            started_at=now_ms(),
            state=SessionState.RECORDING,
            title=spec.title,
            mode=mode,
            source_languages=spec.languages or list(settings.capture.languages),
            target_language=spec.target_language or settings.capture.target_language,
            vocabulary=spec.vocabulary,
            client_user_agent=spec.client_user_agent,
            mic_label=spec.mic_label,
            audio_constraints=spec.audio_constraints,
            cloud_used=not asr.capabilities.local,
            tags=spec.tags,
            participants=spec.participants,
            preset_id=spec.preset_id,
            plugins=spec.plugins,
            local_only=spec.local_only or settings.privacy.local_only,
        )
        await services.repo.create_session(record)
        if spec.preset_id:
            await services.repo.touch_preset(spec.preset_id)

        token = await services.repo.issue_token(record.id, TOKEN_TTL_MS)
        services.bus.seed_seq(record.id, 0)
        services.plugins.set_session_selection(record.id, spec.plugins)

        llm = registry.build_llm(
            settings,
            local_only=record.local_only,
            on_usage=self._usage_recorder(record.id),
        )
        translation = registry.build_translation(settings, llm)
        audio_writer = (
            SessionAudioWriter(
                record.id,
                settings.audio_dir,
                record.started_at,
                codec=settings.audio.codec,
                bitrate_kbps=settings.audio.bitrate_kbps,
            )
            if settings.audio.persist
            else None
        )

        pipeline = SessionPipeline(
            record,
            settings,
            services.repo,
            services.bus,
            on_cost=self._cost_recorder(record.id),
            asr=asr,
            diarization=services.diarization,
            translation=translation,
            translation_fallback=services.translation_fallback,
            audio_writer=audio_writer,
        )
        if spec.min_speakers is not None or spec.max_speakers is not None:
            pipeline._clusterer.min_speakers = spec.min_speakers
            pipeline._clusterer.max_speakers = spec.max_speakers

        await pipeline.start()
        async with self._lock:
            self._sessions[record.id] = ActiveSession(record=record, pipeline=pipeline, llm=llm)
        log.info(
            "session started",
            extra={"session": record.id, "mode": str(mode), "asr": asr.capabilities.name},
        )
        return record, token

    def _check_disk(self) -> None:
        """NFR-RES-7: refuse to start rather than fail part-way through."""
        settings = self._services.settings
        try:
            usage = shutil.disk_usage(settings.server.data_dir)
        except OSError:
            return
        free_mb = usage.free // (1024 * 1024)
        if free_mb < settings.server.min_free_disk_mb:
            raise SessionError(
                f"only {free_mb} MB free at {settings.server.data_dir}, and the configured "
                f"minimum is {settings.server.min_free_disk_mb} MB. Free some space or delete "
                "old sessions before recording."
            )

    def _cost_recorder(self, session_id: str) -> Any:
        """The pipeline's cost hook — recognition bills through here."""

        async def record(component: str, amount_usd: float, provider: str) -> None:
            await self.record_cost(session_id, amount_usd, provider, component)

        return record

    def _usage_recorder(self, session_id: str) -> Any:
        """The LLM's cost hook — translation and plugins bill through here."""

        async def record(usage: Usage, provider: str) -> None:
            await self.record_cost(session_id, usage.cost_usd, provider, "translation")

        return record

    async def record_cost(
        self, session_id: str, amount_usd: float, provider: str, component: str
    ) -> None:
        """Attribute spend to a session and enforce the ceiling (FR-CFG-7).

        Everything that costs money lands here — recognition, translation, and
        plugins — so the ceiling is a *session* budget rather than one budget
        per subsystem, which is what an operator actually means by it.
        """
        if amount_usd <= 0:
            return
        services = self._services
        total, breakdown = await services.repo.add_session_cost(
            session_id, amount_usd, provider, component
        )
        ceiling = services.settings.privacy.session_cost_ceiling_usd
        over = bool(ceiling) and total >= ceiling

        active = self._sessions.get(session_id)
        if over and active is not None and not active.ceiling_hit:
            active.ceiling_hit = True
            await self._degrade_to_local(active)

        await services.bus.publish(
            session_id,
            EventType.STATUS,
            {
                "cost_usd": round(total, 6),
                "cost_breakdown": {k: round(v, 6) for k, v in breakdown.items()},
                "cost_ceiling_usd": ceiling or None,
                "over_ceiling": over,
            },
        )

    async def _degrade_to_local(self, active: ActiveSession) -> None:
        """FR-CFG-7: on reaching the ceiling the session continues locally and
        reports the switch, rather than failing.

        Both halves are switched, because a ceiling that stops paying for
        translation while recognition keeps billing is not a ceiling.
        """
        services = self._services
        session_id = active.record.id
        switched: list[str] = []

        with contextlib.suppress(Exception):
            active.llm.mark_degraded()
            switched.append("translation")

        if not services.asr.capabilities.local:
            local = _local_asr(services.settings)
            if local is not None:
                try:
                    await local.load()
                    active.pipeline.asr = local
                    switched.append("recognition")
                except Exception as exc:
                    log.warning("no local ASR to fall back to at the cost ceiling: %s", exc)

        log.info(
            "session reached its cost ceiling",
            extra={"session": session_id, "switched": ",".join(switched) or "nothing"},
        )
        await services.bus.publish(
            session_id,
            EventType.STATUS,
            {
                "ceiling_reached": True,
                "switched_to_local": switched,
                # Being explicit matters: a ceiling that silently kept spending
                # would be worse than no ceiling at all.
                "note": (
                    "continuing locally"
                    if switched
                    else "no local backend is available, so cloud processing continues"
                ),
            },
        )

    # --- lifecycle ----------------------------------------------------------

    async def attach_ingest(self, session_id: str) -> ActiveSession:
        active = self._sessions.get(session_id)
        if active is None:
            raise SessionError("this session is no longer running")
        if active.grace_task is not None:
            active.grace_task.cancel()
            active.grace_task = None
            log.info("recording client reconnected", extra={"session": session_id})
        active.ingest_connected = True
        return active

    async def detach_ingest(self, session_id: str) -> None:
        """Start the grace period. The session is not ended here — a phone that
        loses wifi for thirty seconds must not lose its meeting (FR-SES-4)."""
        active = self._sessions.get(session_id)
        if active is None or not active.ingest_connected:
            return
        active.ingest_connected = False
        grace = self._services.settings.server.disconnect_grace_s

        async def finalise_later() -> None:
            try:
                await asyncio.sleep(grace)
            except asyncio.CancelledError:
                return
            if session_id in self._sessions and not self._sessions[session_id].ingest_connected:
                log.info(
                    "finalising after disconnect grace period",
                    extra={"session": session_id, "grace_s": grace},
                )
                await self.stop(session_id, reason="client disconnected")

        active.grace_task = asyncio.create_task(finalise_later(), name=f"grace:{session_id}")
        await self._services.bus.publish(
            session_id, EventType.STATUS, {"ingest_connected": False, "grace_s": grace}
        )

    async def stop(self, session_id: str, *, reason: str = "stopped") -> SessionRecord | None:
        async with self._lock:
            active = self._sessions.pop(session_id, None)
        if active is None:
            return await self._services.repo.get_session(session_id)

        if active.grace_task is not None:
            active.grace_task.cancel()
        await active.pipeline.stop()

        # Plugins observe session.end through the bus; wait for their work so
        # that "processing complete" means it (FR-UI-17).
        await self._services.plugins.drain()
        self._services.plugins.release_session(session_id)
        with contextlib.suppress(Exception):
            await active.llm.close()

        record = await self._services.repo.get_session(session_id)
        if record and not record.title:
            title = await self._auto_title(session_id, record)
            if title:
                record.title = title
                await self._services.repo.update_session(session_id, title=title)
        log.info("session ended", extra={"session": session_id, "reason": reason})
        return record

    async def stop_all(self) -> None:
        for session_id in list(self._sessions):
            with contextlib.suppress(Exception):
                await self.stop(session_id, reason="server shutting down")

    async def set_mode(self, session_id: str, mode: LatencyMode) -> None:
        active = self._sessions.get(session_id)
        if active is None:
            raise SessionError("this session is not running")
        report = registry.validate(self._services.settings, self._services.asr, mode)
        if not report.ok:
            raise SessionError("; ".join(report.errors))
        await active.pipeline.request_mode(mode)

    async def _auto_title(self, session_id: str, record: SessionRecord) -> str | None:
        """FR-SES-7. Cheap first: the opening line usually names the meeting
        better than a model would, and costs nothing."""
        utterances = await self._services.repo.list_utterances(session_id, limit=3)
        when = time.strftime("%d %b %H:%M", time.localtime(record.started_at / 1000))
        if not utterances:
            return f"Empty session · {when}"
        return _title_from(utterances[0].text) or f"Session · {when}"


@dataclass
class Services:
    """The application object graph."""

    settings: Settings
    db: Database
    repo: Repository
    search: SearchIndex
    bus: EventBus
    asr: ASRBackend
    diarization: DiarizationBackend | None
    translation_fallback: TranslationBackend | None
    plugins: PluginHost
    sessions: SessionManager = field(init=False)
    validation: registry.ValidationReport = field(default_factory=registry.ValidationReport)
    started_at: float = field(default_factory=time.time)
    #: Extra backends built for `asr.by_language`, keyed by (backend, model).
    _asr_by_language: dict[tuple[str, str], ASRBackend] = field(default_factory=dict)
    _asr_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: loading | ready | failed. Models load in the background so the server can
    #: report on them while it happens.
    model_state: str = "loading"
    model_error: str | None = None
    _load_task: asyncio.Task[None] | None = None
    #: Model downloads this server started, by `backend:model`. Held in memory
    #: on purpose: a download that did not survive a restart did not finish, and
    #: the on-disk check is the authority on everything that did.
    _downloads: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _download_errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.sessions = SessionManager(self)

    # --- construction -------------------------------------------------------

    @classmethod
    async def create(cls, settings: Settings) -> Services:
        settings.ensure_dirs()
        db = Database(settings.db_path)
        await db.connect()
        repo = Repository(db)
        search = SearchIndex(db)
        bus = EventBus()

        asr = registry.build_asr(settings)
        diarization = registry.build_diarization(settings)
        translation_fallback = registry.build_translation_fallback(settings)

        plugin_list, discovery_errors = discover(settings.plugins.directory)
        if settings.plugins.enabled is not None:
            for plugin in plugin_list:
                plugin.enabled = plugin.name in settings.plugins.enabled

        host = PluginHost(
            plugin_list,
            bus=bus,
            store_factory=lambda: RepositoryStore(repo),
            llm_factory=lambda _session_id=None: registry.build_llm(settings),
            artifact_sink=repo.add_artifact,
            state_sink=lambda name, **kw: repo.set_plugin_state(name, **kw),
            timeout_s=settings.plugins.timeout_s,
            errors=discovery_errors,
        )
        host.apply_state(await repo.plugin_states())

        services = cls(
            settings=settings,
            db=db,
            repo=repo,
            search=search,
            bus=bus,
            asr=asr,
            diarization=diarization,
            translation_fallback=translation_fallback,
            plugins=host,
        )

        # The bus persists replayable events and fans them out to plugins. Both
        # happen off the pipeline's thread of control.
        async def sink(event: Any) -> None:
            await repo.append_events(
                [(event.session_id, event.seq, str(event.type), dumps(event.to_json()))]
            )
            await host.dispatch(event.type, event.session_id, event.data)

        bus.set_sink(sink)
        return services

    async def start(self) -> None:
        """Everything that must finish before the server accepts a request.

        Loading models is *not* on this path — see `begin_loading`. On a first
        run that is a multi-gigabyte download, and doing it here meant the
        server bound no socket at all until it finished.
        """
        await self.plugins.start()

        report = registry.validate(self.settings, self.asr)
        self.validation.errors.extend(report.errors)
        self.validation.warnings.extend(report.warnings)
        for message in self.validation.warnings:
            log.warning("configuration: %s", message)

        recovered = await self.repo.recover_orphaned_sessions()
        if recovered:
            log.info("recovered interrupted sessions", extra={"count": len(recovered)})

    async def _load_models(self) -> None:
        self.model_state = "loading"
        try:
            await self.asr.load()
            self.model_state = "ready"
        except Exception as exc:
            self.model_state = "failed"
            self.model_error = str(exc)
            self.validation.errors.append(str(exc))
            log.error("ASR backend failed to load: %s", exc)

        if self.diarization is not None:
            try:
                await self.diarization.load()
            except Exception as exc:
                self.validation.warnings.append(f"diarization unavailable: {exc}")
                log.warning("diarization backend failed to load: %s", exc)
                self.diarization = None

    def begin_loading(self) -> None:
        """Start loading models in the background.

        Called from the request-serving loop rather than from `start()`, because
        the task belongs to whichever loop will later await it — and in tests
        those are not the same loop.

        Loading happens in the background at all because a first run downloads
        several gigabytes, and blocking startup on it meant the server answered
        nothing — not even `/api/health`, whose job is to report model status
        (SRS §8.5). A server that cannot say "still downloading" is
        indistinguishable from a broken one.
        """
        if self._load_task is not None and not self._load_task.done():
            return
        if self.model_state == "ready":
            return
        self._load_task = asyncio.create_task(self._load_models(), name="load-models")
        if self.settings.asr.download_missing:
            self._prefetch()

    def _prefetch(self) -> None:
        """Fetch models this configuration routes to but does not have.

        The default model downloads itself on first load; this covers the rest —
        a per-language route, or anything in `asr.preload` that the operator
        wants available to switch to. In the background, because the whole point
        of the loading rework was that the server keeps answering while weights
        arrive.
        """
        for spec in catalog.required(self.settings):
            if catalog.state(spec, self.settings)[0] == "absent":
                log.info("prefetching missing model", extra={"model": spec.id})
                self.start_model_download(spec)

    async def wait_for_models(self, timeout: float) -> bool:
        """Wait up to `timeout` for background loading to settle. True if ready."""
        self.begin_loading()
        if self._load_task is None or self._load_task.done():
            return self.model_state == "ready"
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(self._load_task), timeout=timeout)
        return self.model_state == "ready"

    def model_status(self) -> dict[str, Any]:
        """What the models are doing, for `/api/health` and the pre-flight check."""
        return {
            "state": self.model_state,
            "backend": self.asr.capabilities.name,
            "error": self.model_error,
            "detail": {
                "loading": (
                    "On a first run this downloads several gigabytes and can take many "
                    "minutes. Recording is unavailable until it finishes; progress is in "
                    "the server log."
                ),
                "failed": self.model_error or "",
                "ready": "",
            }.get(self.model_state, ""),
        }

    # --- model downloads ----------------------------------------------------

    def start_model_download(self, spec: ModelSpec) -> None:
        """Fetch weights in the background, at most one task per model."""
        existing = self._downloads.get(spec.id)
        if existing is not None and not existing.done():
            return
        self._download_errors.pop(spec.id, None)

        async def run() -> None:
            try:
                await asyncio.to_thread(catalog.download, spec, self.settings.models_dir)
                log.info("model downloaded", extra={"model": spec.id})
            except Exception as exc:
                self._download_errors[spec.id] = str(exc)
                log.error("model download failed: %s", exc, extra={"model": spec.id})

        self._downloads[spec.id] = asyncio.create_task(run(), name=f"download:{spec.id}")

    def download_state(self, model_id: str) -> str | None:
        """`downloading` | `failed`, or None to defer to what is on disk."""
        task = self._downloads.get(model_id)
        if task is not None and not task.done():
            return "downloading"
        return "failed" if model_id in self._download_errors else None

    def download_error(self, model_id: str) -> str | None:
        return self._download_errors.get(model_id)

    def tracked_downloads(self) -> list[str]:
        """Every model this server has tried to fetch since it started."""
        return sorted(set(self._downloads) | set(self._download_errors))

    async def shutdown(self) -> None:
        for task in self._downloads.values():
            task.cancel()
        if self._load_task is not None:
            self._load_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._load_task
        await self.sessions.stop_all()
        await self.plugins.stop()
        for backend in self._asr_by_language.values():
            with contextlib.suppress(Exception):
                await backend.close()
        self._asr_by_language.clear()
        with contextlib.suppress(Exception):
            await self.asr.close()
        if self.diarization is not None:
            with contextlib.suppress(Exception):
                await self.diarization.close()
        await self.db.close()

    async def asr_for(self, language: str | None) -> ASRBackend:
        """The loaded ASR backend for a language, building it once.

        Backends are cached by `(backend, model)`: loading weights costs seconds
        and often gigabytes, and a per-language setup would otherwise pay that
        again on every session that switches language.
        """
        key = self.settings.asr.for_language(language)
        if key == (self.settings.asr.backend, self.settings.asr.model):
            return self.asr
        async with self._asr_lock:
            cached = self._asr_by_language.get(key)
            if cached is None:
                log.info(
                    "loading ASR for language",
                    extra={"language": language, "backend": key[0], "model": key[1]},
                )
                cached = registry.build_asr(self.settings, language)
                await cached.load()
                self._asr_by_language[key] = cached
            return cached

    async def _evict_unreachable_asr(self) -> None:
        """Drop per-language backends the new routing can no longer reach.

        Each cached entry is a loaded model holding hundreds of megabytes. Left
        alone, re-routing Russian from GigaAM to Whisper three times over an
        afternoon leaves three models resident and nothing using two of them.

        Running sessions hold their own reference, so a backend dropped here
        stays alive for as long as someone is mid-recording on it.
        """
        reachable = {
            self.settings.asr.for_language(code) for code in self.settings.capture.languages
        }
        reachable.add((self.settings.asr.backend, self.settings.asr.model))
        for key in [k for k in self._asr_by_language if k not in reachable]:
            backend = self._asr_by_language.pop(key)
            log.info("releasing ASR backend", extra={"backend": key[0], "model": key[1]})
            with contextlib.suppress(Exception):
                await backend.close()

    async def reconfigure(self, settings: Settings) -> None:
        """Swap backends for the next session without a restart (FR-CFG-8).

        Running sessions hold their own references, so they finish on the
        backend they started with — switching a model out from under a live
        pipeline is exactly the kind of surprise this feature is meant to avoid.
        The old backend is closed only when nothing is using it.
        """
        old_asr, old_diarization = self.asr, self.diarization
        asr_changed = (
            settings.asr.backend != self.settings.asr.backend
            or settings.asr.model != self.settings.asr.model
        )
        diarization_changed = (
            settings.diarization.backend != self.settings.diarization.backend
            or settings.diarization.enabled != self.settings.diarization.enabled
        )

        new_asr = registry.build_asr(settings) if asr_changed else self.asr
        new_diarization = (
            registry.build_diarization(settings) if diarization_changed else self.diarization
        )
        if asr_changed:
            await new_asr.load()
            # The new backend is loaded, so the session guard must stop citing
            # the old one's progress — otherwise switching to a model already on
            # disk still refuses recordings until the original download finishes.
            self.model_state = "ready"
            self.model_error = None
        if diarization_changed and new_diarization is not None:
            await new_diarization.load()

        self.settings = settings
        self.asr = new_asr
        self.diarization = new_diarization
        self.translation_fallback = registry.build_translation_fallback(settings)
        await self._evict_unreachable_asr()

        report = registry.validate(settings, new_asr)
        self.validation = report

        if asr_changed and not self.sessions.active_ids:
            with contextlib.suppress(Exception):
                await old_asr.close()
        if diarization_changed and old_diarization is not None and not self.sessions.active_ids:
            with contextlib.suppress(Exception):
                await old_diarization.close()
        log.info(
            "reconfigured",
            extra={"asr": new_asr.capabilities.name, "local_only": settings.privacy.local_only},
        )

    # --- helpers used by routes --------------------------------------------

    def llm(self) -> LLMClient:
        return registry.build_llm(self.settings)

    async def emit_artifact(
        self, session_id: str, plugin_name: str, version: str, artifact: Artifact
    ) -> dict[str, Any]:
        return await self.repo.add_artifact(session_id, plugin_name, version, artifact)

    def disk(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.settings.server.data_dir)
        except OSError:
            return {"available": False}
        free_mb = usage.free // (1024 * 1024)
        return {
            "available": True,
            "path": str(self.settings.server.data_dir),
            "free_mb": free_mb,
            "total_mb": usage.total // (1024 * 1024),
            "min_free_mb": self.settings.server.min_free_disk_mb,
            "warn_free_mb": self.settings.server.warn_free_disk_mb,
            "below_minimum": free_mb < self.settings.server.min_free_disk_mb,
            "low": free_mb < self.settings.server.warn_free_disk_mb,
        }


#: A title has to fit a history row and a header. Long enough to identify the
#: conversation, short enough not to become the interface.
TITLE_MAX_CHARS = 48


def _title_from(text: str) -> str:
    """First sentence of the opening line, cut at a word boundary.

    The opening sentence names the meeting better than a model would, and costs
    nothing (FR-SES-7).
    """
    import re

    sentence = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0].strip()
    sentence = sentence.rstrip(".!?,;: ")
    if len(sentence) <= TITLE_MAX_CHARS:
        return sentence
    clipped = sentence[:TITLE_MAX_CHARS].rsplit(" ", 1)[0]
    return f"{clipped}…"


def _local_asr(settings: Settings) -> Any:
    """A local ASR backend to fall back to, or None if none is configured.

    Deliberately does not guess a model: it uses whatever `asr.model` names,
    which is the model the operator already chose to have on disk.
    """
    from ..backends.asr.faster_whisper import FasterWhisperBackend

    try:
        return FasterWhisperBackend(settings.asr, settings.models_dir)
    except Exception:
        return None


def data_paths(settings: Settings) -> dict[str, Path]:
    return {
        "data_dir": settings.server.data_dir,
        "db": settings.db_path,
        "audio": settings.audio_dir,
        "models": settings.models_dir,
    }
