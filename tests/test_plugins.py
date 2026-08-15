"""Plugin host: discovery, isolation, timeouts, versioning (FR-PLG-*).

The headline requirement is FR-PLG-3 — a crashing plugin must cost its own
output and nothing else. The session completes, the other plugins run, and the
UI reports the failure.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from droid_assistant.domain import Artifact
from droid_assistant.events import EventBus, EventType
from droid_assistant.plugins.api import Context, Event, Plugin, format_transcript
from droid_assistant.plugins.host import PluginHost, discover

# --- test doubles -----------------------------------------------------------


class GoodConfig(BaseModel):
    prefix: str = "ok"
    repeat: int = 1


class GoodPlugin(Plugin):
    name = "good"
    version = "1.0.0"
    api_version = 1
    config_schema = GoodConfig
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact:
        return Artifact(kind="good", content=ctx.config.prefix * ctx.config.repeat)


class CrashingPlugin(Plugin):
    name = "crashing"
    version = "1.0.0"
    api_version = 1
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact:
        raise RuntimeError("deliberately broken")


class SlowPlugin(Plugin):
    name = "slow"
    version = "1.0.0"
    api_version = 1
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact:
        await asyncio.sleep(30)
        return Artifact(kind="slow", content="never reached")


class WrongVersionPlugin(Plugin):
    name = "future"
    version = "2.0.0"
    api_version = 99
    subscribes = {Event.SESSION_END}


class Store:
    async def transcript(self, session_id: str, **kwargs: object) -> str:
        return "Anna: hello\nMarko: hi"

    async def utterances(self, session_id: str) -> list:
        return []

    async def speakers(self, session_id: str) -> list:
        return []

    async def session_metadata(self, session_id: str) -> dict:
        return {"title": "test"}


class FakeLLM:
    available = True

    async def complete(self, prompt: str, **kwargs: object) -> str:
        return "a summary"


class UnavailableLLM:
    available = False
    reason = "no credential configured"

    async def complete(self, prompt: str, **kwargs: object) -> str:
        raise RuntimeError(self.reason)


@pytest.fixture
def harness():
    bus = EventBus()
    artifacts: list[tuple[str, str, Artifact]] = []
    errors: list[dict] = []

    async def sink(session_id: str, plugin: str, version: str, artifact: Artifact) -> dict:
        artifacts.append((session_id, plugin, artifact))
        return {
            "id": f"art_{len(artifacts)}",
            "kind": artifact.kind,
            "mime": artifact.mime,
            "version": len(artifacts),
            "content": artifact.content,
        }

    async def state_sink(name: str, **kwargs: object) -> None:
        errors.append({"plugin": name, **kwargs})

    def build(plugins, llm=None):
        from droid_assistant.plugins.host import LoadedPlugin

        host = PluginHost(
            [LoadedPlugin(instance=p, source="test") for p in plugins],
            bus=bus,
            store_factory=Store,
            llm_factory=lambda _session_id=None: llm or FakeLLM(),
            artifact_sink=sink,
            state_sink=state_sink,
            timeout_s=0.3,
        )
        return host

    return {"build": build, "bus": bus, "artifacts": artifacts, "errors": errors}


# --- tests ------------------------------------------------------------------


class TestIsolation:
    async def test_a_crashing_plugin_does_not_stop_the_others(self, harness) -> None:
        """FR-PLG-3, the requirement this whole design exists for."""
        host = harness["build"]([CrashingPlugin(), GoodPlugin()])
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=5)
        finally:
            await host.stop()

        kinds = [artifact.kind for _sid, _name, artifact in harness["artifacts"]]
        assert "good" in kinds  # the healthy plugin still produced its output
        assert not any(k == "crashing" for k in kinds)
        assert any(e["plugin"] == "crashing" and "error" in e for e in harness["errors"])

    async def test_a_failure_is_reported_on_the_event_stream(self, harness) -> None:
        host = harness["build"]([CrashingPlugin()])
        subscriber = harness["bus"].subscribe("sess_1")
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=5)
        finally:
            await host.stop()

        seen = []
        while True:
            try:
                seen.append(subscriber._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        errors = [e for e in seen if e and e.type is EventType.PLUGIN_ERROR]
        assert errors
        assert errors[0].data["plugin"] == "crashing"
        # FR-UI-9: the error says what to do about it.
        assert errors[0].data["remedy"]

    async def test_a_plugin_is_disabled_only_for_the_failing_session(self, harness) -> None:
        host = harness["build"]([CrashingPlugin()])
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=5)
            first = len(harness["errors"])

            # A different session gets a fresh chance.
            await host.dispatch(EventType.SESSION_END, "sess_2", {})
            await host.drain(timeout=5)
            assert len(harness["errors"]) > first

            # The same session does not retry.
            before = len(harness["errors"])
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=5)
            assert len(harness["errors"]) == before
        finally:
            await host.stop()

    async def test_a_slow_plugin_is_cancelled_at_its_timeout(self, harness) -> None:
        """FR-PLG-4: bounded, cancelled, reported, session unaffected."""
        host = harness["build"]([SlowPlugin(), GoodPlugin()])
        await host.start(workers=2)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await asyncio.wait_for(host.drain(timeout=5), timeout=5)
        finally:
            await host.stop()

        assert any("timed out" in str(e.get("error", "")) for e in harness["errors"])
        assert any(artifact.kind == "good" for _s, _n, artifact in harness["artifacts"])


class TestSubscriptionAndConfig:
    async def test_only_subscribed_events_are_delivered(self, harness) -> None:
        """FR-PLG-2: a plugin subscribing only to session.end is not invoked
        for anything else."""
        host = harness["build"]([GoodPlugin()])
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.UTTERANCE_FINAL, "sess_1", {})
            await host.drain(timeout=2)
            assert harness["artifacts"] == []

            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=2)
            assert len(harness["artifacts"]) == 1
        finally:
            await host.stop()

    async def test_configuration_is_validated_against_the_declared_schema(self, harness) -> None:
        host = harness["build"]([GoodPlugin()])
        assert host.validate_config("good", {"prefix": "x", "repeat": 3}) == {
            "prefix": "x",
            "repeat": 3,
        }
        with pytest.raises(ValueError, match="repeat"):
            host.validate_config("good", {"repeat": "not a number"})

    async def test_configuration_reaches_the_handler(self, harness) -> None:
        host = harness["build"]([GoodPlugin()])
        host.plugins["good"].config = {"prefix": "ab", "repeat": 3}
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=2)
        finally:
            await host.stop()
        assert harness["artifacts"][0][2].content == "ababab"

    async def test_a_disabled_plugin_is_not_dispatched(self, harness) -> None:
        """FR-PLG-6: toggling takes effect without restarting the server."""
        host = harness["build"]([GoodPlugin()])
        host.set_enabled("good", False)
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=2)
        finally:
            await host.stop()
        assert harness["artifacts"] == []

    async def test_per_session_plugin_selection_is_honoured(self, harness) -> None:
        host = harness["build"]([GoodPlugin(), CrashingPlugin()])
        host.set_session_selection("sess_1", ["good"])
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=2)
        finally:
            await host.stop()
        assert harness["errors"] == []
        assert len(harness["artifacts"]) == 1

    async def test_a_plugin_needing_an_unavailable_llm_is_skipped(self, harness) -> None:
        """FR-CFG-5: cloud-dependent plugins report as unavailable rather than
        failing identically on every session."""

        class NeedsLLM(GoodPlugin):
            name = "needs_llm"
            requires_llm = True

        host = harness["build"]([NeedsLLM()], llm=UnavailableLLM())
        assert host.unavailable_reason(host.plugins["needs_llm"]) == "no credential configured"
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=2)
        finally:
            await host.stop()
        assert harness["artifacts"] == []
        assert harness["errors"] == []  # skipped, not failed

    def test_the_listing_exposes_a_schema_the_ui_can_render(self, harness) -> None:
        """FR-PLG-5: three typed fields become three correctly-typed inputs."""
        host = harness["build"]([GoodPlugin()])
        entry = host.listing()[0]
        properties = entry["config_schema"]["properties"]
        assert properties["prefix"]["type"] == "string"
        assert properties["repeat"]["type"] == "integer"


class TestDiscovery:
    def test_builtin_plugins_are_found(self) -> None:
        plugins, errors = discover(None)
        names = {plugin.name for plugin in plugins}
        assert {"summary", "action_items"} <= names
        assert not errors

    def test_a_dropped_in_file_is_loaded(self, tmp_path: Path) -> None:
        """FR-PLG-1: a file in plugins/ loads on restart with no other change."""
        (tmp_path / "hello.py").write_text(
            "from droid_assistant.plugins import Artifact, Context, Event, Plugin\n"
            "\n"
            "class HelloPlugin(Plugin):\n"
            "    name = 'hello'\n"
            "    version = '1.0.0'\n"
            "    api_version = 1\n"
            "    subscribes = {Event.SESSION_END}\n"
            "\n"
            "    async def on_session_end(self, ctx):\n"
            "        return Artifact(kind='hello', content='hi')\n",
            encoding="utf-8",
        )
        plugins, errors = discover(tmp_path, include_builtin=False)
        assert [plugin.name for plugin in plugins] == ["hello"]
        assert not errors

    def test_a_broken_file_is_reported_and_the_rest_still_load(self, tmp_path: Path) -> None:
        (tmp_path / "broken.py").write_text("this is not python(", encoding="utf-8")
        (tmp_path / "fine.py").write_text(
            "from droid_assistant.plugins import Event, Plugin\n"
            "class FinePlugin(Plugin):\n"
            "    name = 'fine'\n"
            "    api_version = 1\n"
            "    subscribes = {Event.SESSION_END}\n",
            encoding="utf-8",
        )
        plugins, errors = discover(tmp_path, include_builtin=False)
        assert [plugin.name for plugin in plugins] == ["fine"]
        assert any("broken.py" in error for error in errors)

    def test_a_mismatched_api_version_is_refused_with_a_reason(self, tmp_path: Path) -> None:
        """NFR-MNT-2: a major-version mismatch means the Context the plugin
        expects is not the one it would get."""
        from droid_assistant.plugins.host import LoadedPlugin  # noqa: F401

        (tmp_path / "future.py").write_text(
            "from droid_assistant.plugins import Event, Plugin\n"
            "class FuturePlugin(Plugin):\n"
            "    name = 'future'\n"
            "    api_version = 99\n"
            "    subscribes = {Event.SESSION_END}\n",
            encoding="utf-8",
        )
        plugins, errors = discover(tmp_path, include_builtin=False)
        assert plugins == []
        assert any("api_version 99" in error for error in errors)


class TestTranscriptFormatting:
    def test_speakers_timestamps_and_translations_render(self) -> None:
        from droid_assistant.domain import Speaker, Utterance

        speakers = {
            "s1": Speaker(id="s1", session_id="x", label="Speaker 1", index=0, display_name="Anna")
        }
        utterances = [
            Utterance(
                id="u1",
                session_id="x",
                seq=0,
                start_ms=65_000,
                end_ms=68_000,
                text="Нам нужно закончить",
                speaker_id="s1",
                translation="We need to finish",
            )
        ]
        rendered = format_transcript(utterances, speakers, include_translation=True)
        assert "[01:05]" in rendered
        assert "Anna:" in rendered
        assert "→ We need to finish" in rendered

    def test_unattributed_utterances_render_as_unknown(self) -> None:
        from droid_assistant.domain import Utterance

        rendered = format_transcript(
            [Utterance(id="u", session_id="x", seq=0, start_ms=0, end_ms=1000, text="hello")], {}
        )
        assert "Unknown: hello" in rendered
