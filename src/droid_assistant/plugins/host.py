"""The plugin host: discovery, supervision, isolation (FR-PLG-1 … FR-PLG-9).

The design goal is that a bad plugin costs its own output and nothing else. Four
mechanisms deliver that:

* Handlers run in **tasks off the live path**, fed from a queue. A plugin taking
  60 seconds delays no utterance (FR-PLG-9).
* Every invocation has a **timeout** and is cancelled past it (FR-PLG-4).
* An exception **disables that plugin for that session only**, and is reported
  through the event stream and the UI (FR-PLG-3).
* Discovery failures are per-plugin: a module that will not import is reported,
  and the others load.

What the host cannot do is contain a plugin that decides to delete files. It
runs in-process with full privileges (NFR-SEC-6).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.metadata
import importlib.util
import inspect
import logging
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from .. import PLUGIN_API_VERSION
from ..domain import Artifact
from ..events import EventBus, EventType
from .api import Context, Event, Plugin

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "droid_assistant.plugins"

#: Wire event → plugin event. Types absent here are never dispatched to plugins.
_EVENT_MAP = {
    EventType.SESSION_START: Event.SESSION_START,
    EventType.SESSION_END: Event.SESSION_END,
    EventType.UTTERANCE_FINAL: Event.UTTERANCE_FINAL,
    EventType.TRANSLATION_FINAL: Event.TRANSLATION_FINAL,
    EventType.SPEAKER_CHANGED: Event.SPEAKER_CHANGED,
    EventType.TRANSCRIPT_EDITED: Event.TRANSCRIPT_EDITED,
}


@dataclass
class LoadedPlugin:
    instance: Plugin
    source: str  # "builtin", "directory:<path>", or "entry_point:<dist>"
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    last_error_at: int | None = None
    #: Sessions in which this plugin has failed and is therefore skipped for the
    #: remainder — FR-PLG-3's "for the session" scoping.
    failed_sessions: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.instance.name

    def to_json(self, *, unavailable_reason: str | None = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.instance.version,
            "api_version": self.instance.api_version,
            "description": self.instance.description,
            "source": self.source,
            "enabled": self.enabled,
            "subscribes": sorted(str(e) for e in self.instance.subscribes),
            "requires_llm": self.instance.requires_llm,
            "config": self.config,
            "config_schema": self.instance.config_json_schema(),
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "available": unavailable_reason is None,
            "unavailable_reason": unavailable_reason,
        }


class DiscoveryError(Exception):
    """One plugin failed to load. Reported; never fatal."""


def discover(
    directory: Path | None, *, include_builtin: bool = True
) -> tuple[list[LoadedPlugin], list[str]]:
    """Find plugins. Returns `(loaded, errors)` — errors are per-plugin strings."""
    loaded: list[LoadedPlugin] = []
    errors: list[str] = []
    seen: set[str] = set()

    def register(plugin: Plugin, source: str) -> None:
        if not plugin.name:
            errors.append(f"{source}: plugin class has no `name`; skipped")
            return
        if plugin.api_version != PLUGIN_API_VERSION:
            errors.append(
                f"{plugin.name} ({source}) declares api_version {plugin.api_version}, but this "
                f"server implements {PLUGIN_API_VERSION}. Refusing to load it — a mismatched "
                "major version means the Context it expects is not the one it would get."
            )
            return
        if plugin.name in seen:
            errors.append(f"{plugin.name} ({source}): a plugin with this name is already loaded")
            return
        seen.add(plugin.name)
        loaded.append(LoadedPlugin(instance=plugin, source=source))

    if include_builtin:
        for module_name in ("summary", "action_items"):
            try:
                module = importlib.import_module(f".builtin.{module_name}", package=__package__)
                for plugin in _plugins_in(module):
                    register(plugin, "builtin")
            except Exception as exc:
                errors.append(f"builtin.{module_name}: {exc}")

    # Entry points from installed packages (FR-PLG-1).
    try:
        for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
            try:
                target = entry.load()
                plugin = cast(Plugin, target() if inspect.isclass(target) else target)
                register(plugin, f"entry_point:{entry.name}")
            except Exception as exc:
                errors.append(f"entry point {entry.name}: {exc}")
    except Exception as exc:
        errors.append(f"entry-point scan failed: {exc}")

    # Drop-in directory (FR-PLG-1): a file dropped in loads on restart.
    if directory and directory.is_dir():
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                for plugin in _plugins_in(_load_module(path)):
                    register(plugin, f"directory:{path.name}")
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

    return loaded, errors


def _plugins_in(module: Any) -> list[Plugin]:
    found: list[Plugin] = []
    for obj in vars(module).values():
        if inspect.isclass(obj) and issubclass(obj, Plugin) and obj is not Plugin:
            if obj.__module__ != module.__name__:
                continue  # imported, not defined here
            found.append(obj())
    return found


def _load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"droid_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise DiscoveryError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(slots=True)
class _Job:
    plugin: LoadedPlugin
    event: Event
    session_id: str
    payload: dict[str, Any]


class PluginHost:
    """Dispatches events to plugins, off the live path."""

    def __init__(
        self,
        plugins: list[LoadedPlugin],
        *,
        bus: EventBus,
        store_factory: Any,  # () -> PluginStore
        llm_factory: Any,  # (session_id) -> LLM
        artifact_sink: Any,  # async (session_id, plugin, version, Artifact) -> dict
        state_sink: Any = None,  # async (name, *, error=..., clear_error=...) -> None
        timeout_s: float = 180.0,
        errors: list[str] | None = None,
    ) -> None:
        self.plugins = {p.name: p for p in plugins}
        self.discovery_errors = errors or []
        self._bus = bus
        self._store_factory = store_factory
        self._llm_factory = llm_factory
        self._artifact_sink = artifact_sink
        self._state_sink = state_sink
        self._timeout_s = timeout_s
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue(maxsize=4096)
        self._workers: list[asyncio.Task[None]] = []
        #: Per-session plugin selection, from `sessions.plugins`.
        self._session_selection: dict[str, set[str] | None] = {}

    # --- lifecycle ----------------------------------------------------------

    async def start(self, workers: int = 2) -> None:
        for i in range(workers):
            self._workers.append(asyncio.create_task(self._worker(), name=f"plugin-worker-{i}"))
        for message in self.discovery_errors:
            log.warning("plugin discovery: %s", message)

    async def stop(self) -> None:
        for _ in self._workers:
            await self._queue.put(None)
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=self._timeout_s + 10)
        self._workers.clear()

    async def drain(self, timeout: float = 300.0) -> None:
        """Wait for queued work to finish — used before a session is reported done."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout=timeout)

    # --- configuration ------------------------------------------------------

    def apply_state(self, state: dict[str, dict[str, Any]]) -> None:
        """Merge persisted enable/disable and config (FR-PLG-6)."""
        for name, plugin in self.plugins.items():
            saved = state.get(name)
            if not saved:
                continue
            plugin.enabled = bool(saved.get("enabled", True))
            plugin.config = dict(saved.get("config") or {})
            plugin.last_error = saved.get("last_error")
            plugin.last_error_at = saved.get("last_error_at")

    def set_enabled(self, name: str, enabled: bool) -> None:
        if plugin := self.plugins.get(name):
            plugin.enabled = enabled

    def validate_config(self, name: str, config: dict[str, Any]) -> dict[str, Any]:
        """Validate against the plugin's own schema, raising a readable error."""
        plugin = self.plugins.get(name)
        if plugin is None:
            raise KeyError(name)
        try:
            model = plugin.instance.config_schema(**config)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise ValueError(f"invalid configuration for {name}: {details}") from exc
        return model.model_dump()

    def set_session_selection(self, session_id: str, names: list[str] | None) -> None:
        self._session_selection[session_id] = set(names) if names is not None else None

    def release_session(self, session_id: str) -> None:
        self._session_selection.pop(session_id, None)
        for plugin in self.plugins.values():
            plugin.failed_sessions.discard(session_id)

    def unavailable_reason(self, plugin: LoadedPlugin, session_id: str | None = None) -> str | None:
        if not plugin.enabled:
            return "disabled"
        if plugin.instance.requires_llm:
            llm = self._llm_factory(session_id)
            if not getattr(llm, "available", True):
                return getattr(llm, "reason", "the configured LLM is unavailable")
        return None

    def listing(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            plugin.to_json(unavailable_reason=self.unavailable_reason(plugin, session_id))
            for plugin in sorted(self.plugins.values(), key=lambda p: p.name)
        ]

    # --- dispatch -----------------------------------------------------------

    async def dispatch(
        self, event_type: EventType, session_id: str, payload: dict[str, Any]
    ) -> None:
        """Called from the event bus. Returns immediately — never blocks the
        pipeline (FR-PLG-9)."""
        plugin_event = _EVENT_MAP.get(event_type)
        if plugin_event is None:
            return
        selection = self._session_selection.get(session_id)
        for plugin in self.plugins.values():
            if not plugin.enabled or plugin_event not in plugin.instance.subscribes:
                continue
            if selection is not None and plugin.name not in selection:
                continue
            if session_id in plugin.failed_sessions:
                continue
            if (reason := self.unavailable_reason(plugin, session_id)) is not None:
                # FR-CFG-5: a plugin whose dependency is switched off reports as
                # unavailable. Dispatching it would produce an identical failure
                # on every session, logged as if something had gone wrong.
                log.debug(
                    "skipping unavailable plugin", extra={"plugin": plugin.name, "reason": reason}
                )
                continue
            job = _Job(plugin=plugin, event=plugin_event, session_id=session_id, payload=payload)
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                log.warning(
                    "plugin queue is full; dropping a dispatch",
                    extra={"plugin": plugin.name, "event": str(plugin_event)},
                )

    async def run_now(self, name: str, session_id: str) -> dict[str, Any] | None:
        """Re-run one plugin's `session.end` handler over the current transcript.

        This is what `POST /api/sessions/{id}/plugins/{name}/run` calls after a
        transcript edit (FR-SES-9, FR-PLG-12) — synchronous, because the caller
        is waiting for the new artifact.
        """
        plugin = self.plugins.get(name)
        if plugin is None:
            raise KeyError(name)
        plugin.failed_sessions.discard(session_id)
        artifact = await self._invoke(
            _Job(plugin, Event.SESSION_END, session_id, {"reason": "manual"}), raise_errors=True
        )
        return artifact

    # --- worker -------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    return
                await self._invoke(job)
            finally:
                self._queue.task_done()

    async def _invoke(self, job: _Job, *, raise_errors: bool = False) -> dict[str, Any] | None:
        plugin = job.plugin
        handler = plugin.instance.handler_for(job.event)
        if handler is None:
            return None

        emitted: list[dict[str, Any]] = []

        async def emit(artifact: Artifact) -> None:
            record = await self._artifact_sink(
                job.session_id, plugin.name, plugin.instance.version, artifact
            )
            emitted.append(record)
            await self._bus.publish(
                job.session_id,
                EventType.ARTIFACT_CREATED,
                {
                    "artifact_id": record["id"],
                    "plugin": plugin.name,
                    "kind": record["kind"],
                    "version": record["version"],
                    "mime": record["mime"],
                },
            )

        try:
            config = plugin.instance.config_schema(**plugin.config)
        except ValidationError as exc:
            await self._fail(plugin, job.session_id, f"invalid configuration: {exc}", raise_errors)
            return None

        ctx = Context(
            session_id=job.session_id,
            store=self._store_factory(),
            llm=self._llm_factory(job.session_id),
            config=config,
            logger=logging.getLogger(f"droid_assistant.plugins.{plugin.name}"),
            event=job.event,
            payload=job.payload,
            _emit=emit,
        )

        await self._bus.publish(
            job.session_id,
            EventType.PLUGIN_STARTED,
            {"plugin": plugin.name, "event": str(job.event)},
        )

        try:
            result = await asyncio.wait_for(handler(ctx), timeout=self._timeout_s)
        except TimeoutError:
            await self._fail(
                plugin,
                job.session_id,
                f"timed out after {self._timeout_s:.0f}s handling {job.event}",
                raise_errors,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            log.warning(
                "plugin raised",
                extra={"plugin": plugin.name, "event": str(job.event)},
                exc_info=True,
            )
            await self._fail(plugin, job.session_id, detail, raise_errors, traceback.format_exc())
            return None

        if isinstance(result, Artifact):
            await emit(result)
        if plugin.last_error and self._state_sink is not None:
            plugin.last_error = None
            await self._state_sink(plugin.name, clear_error=True)
        return emitted[-1] if emitted else None

    async def _fail(
        self,
        plugin: LoadedPlugin,
        session_id: str,
        message: str,
        raise_errors: bool,
        detail: str | None = None,
    ) -> None:
        """FR-PLG-3: disable for this session, report, and carry on."""
        plugin.failed_sessions.add(session_id)
        plugin.last_error = message
        if self._state_sink is not None:
            with contextlib.suppress(Exception):
                await self._state_sink(plugin.name, error=message)
        await self._bus.publish(
            session_id,
            EventType.PLUGIN_ERROR,
            {
                "plugin": plugin.name,
                "error": message,
                "remedy": f"check the {plugin.name} settings, then re-run it from the session view",
            },
        )
        if detail:
            log.debug("plugin traceback\n%s", detail)
        if raise_errors:
            raise RuntimeError(message)
