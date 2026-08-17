"""Plugin host: discovery, isolation, timeouts, versioning (FR-PLG-*).

The headline requirement is FR-PLG-3 — a crashing plugin must cost its own
output and nothing else. The session completes, the other plugins run, and the
UI reports the failure.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from droid_assistant.domain import Artifact, Utterance
from droid_assistant.events import EventBus, EventType
from droid_assistant.plugins.api import Context, Event, Plugin, Prompt, format_transcript
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


def an_utterance(utterance_id: str, text: str, seq: int = 0) -> Utterance:
    return Utterance(
        id=utterance_id,
        session_id="sess_1",
        seq=seq,
        start_ms=seq * 1_000,
        end_ms=seq * 1_000 + 900,
        text=text,
    )


class Store:
    """A session of three lines, and whatever artifacts a test has planted."""

    LINES = (
        an_utterance("utt_1", "давайте начнем", 0),
        an_utterance("utt_2", "я пришлю цифры до пятницы", 1),
        an_utterance("utt_3", "я забронирую переговорку", 2),
    )

    def __init__(self, artifacts: list[dict] | None = None) -> None:
        self._artifacts = artifacts or []

    async def transcript(self, session_id: str, **kwargs: object) -> str:
        return "Anna: hello\nMarko: hi"

    async def utterances(self, session_id: str) -> list:
        return list(self.LINES)

    async def speakers(self, session_id: str) -> list:
        return []

    async def session_metadata(self, session_id: str) -> dict:
        return {"title": "test"}

    async def artifacts(self, session_id: str, *, kind: str | None = None) -> list[dict]:
        if kind is None:
            return list(self._artifacts)
        return [a for a in self._artifacts if a["kind"] == kind]


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
            # The real sink stores and returns this, and the UI reads it to say
            # what a run actually did.
            "metadata": artifact.metadata,
        }

    async def state_sink(name: str, **kwargs: object) -> None:
        errors.append({"plugin": name, **kwargs})

    def build(plugins, llm=None, store_artifacts=None):
        from droid_assistant.plugins.host import LoadedPlugin

        host = PluginHost(
            [LoadedPlugin(instance=p, source="test") for p in plugins],
            bus=bus,
            store_factory=lambda: Store(store_artifacts),
            llm_factory=lambda _session_id=None: llm or FakeLLM(),
            artifact_sink=sink,
            state_sink=state_sink,
            timeout_s=0.3,
        )
        return host

    return {"build": build, "bus": bus, "artifacts": artifacts, "errors": errors}


async def ran(host, *args, **kwargs):
    """`run_now` returns `(artifact, why-not)`. Most tests want only the first."""
    artifact, _reason = await host.run_now(*args, **kwargs)
    return artifact


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


# --- running over part of a session ------------------------------------------


class RecordingPlugin(Plugin):
    """Reports what it was given to look at, so scoping can be asserted."""

    name = "recorder"
    version = "1.0.0"
    api_version = 1
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact:
        seen = await ctx.utterances()
        return Artifact(kind="seen", content="|".join(u.id for u in seen))


class TestScopedRuns:
    """ "Make an action item out of *this*" is one plugin run over one line.

    The event stays `session.end` — the handler that summarises is the one that
    should answer — and what changes is how much of the session it can see.
    """

    async def test_an_unscoped_run_sees_the_whole_session(self, harness) -> None:
        host = harness["build"]([RecordingPlugin()])
        artifact = await ran(host, "recorder", "sess_1")
        assert artifact["content"] == "utt_1|utt_2|utt_3"

    async def test_a_scoped_run_sees_only_the_lines_it_names(self, harness) -> None:
        host = harness["build"]([RecordingPlugin()])
        artifact = await ran(host, "recorder", "sess_1", utterance_ids=["utt_2"])
        assert artifact["content"] == "utt_2"

    async def test_scope_survives_as_the_order_the_session_has(self, harness) -> None:
        """Ids arrive in whatever order the caller listed them; the transcript
        handed to a model must still read forwards."""
        host = harness["build"]([RecordingPlugin()])
        artifact = await ran(host, "recorder", "sess_1", utterance_ids=["utt_3", "utt_1"])
        assert artifact["content"] == "utt_1|utt_3"

    async def test_an_unknown_id_scopes_to_nothing_rather_than_everything(self, harness) -> None:
        """The route rejects these before they get here. If one ever did, the
        failure has to be visibly empty rather than a whole-session answer to a
        question about one line."""
        host = harness["build"]([RecordingPlugin()])
        artifact = await ran(host, "recorder", "sess_1", utterance_ids=["utt_nope"])
        assert artifact["content"] == ""


class ItemsLLM:
    """Returns one action item, named after the line it was given."""

    available = True

    def __init__(self, text: str = "Book the room") -> None:
        self.text = text
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete(self, prompt: str, **kwargs: object) -> str:
        self.prompts.append(prompt)
        self.systems.append(str(kwargs.get("system") or ""))
        return json.dumps([{"text": self.text, "owner": None, "due": None, "quote": "q"}])


def planted(items: list[dict]) -> list[dict]:
    return [
        {
            "kind": "action_items_json",
            "current": True,
            "content": json.dumps(items),
        }
    ]


class TestActionItemsFromOneMessage:
    def plugin(self):
        from droid_assistant.plugins.builtin.action_items import ActionItemsPlugin

        return ActionItemsPlugin()

    async def test_a_scoped_run_adds_to_the_list_rather_than_replacing_it(self, harness) -> None:
        existing = [{"text": "Send the Q3 numbers", "owner": "Anna", "due": None, "quote": "q"}]
        host = harness["build"](
            [self.plugin()], llm=ItemsLLM("Book the room"), store_artifacts=planted(existing)
        )
        artifact = await ran(host, "action_items", "sess_1", utterance_ids=["utt_3"])

        assert "Send the Q3 numbers" in artifact["content"]
        assert "Book the room" in artifact["content"]

    async def test_the_same_line_twice_does_not_double_the_list(self, harness) -> None:
        """Pressing the button twice on one message is an ordinary accident.

        Nothing new means nothing filed: a second version identical to the first
        is noise in the history and, worse, tells whoever pressed the button
        that something happened.
        """
        existing = [
            {"text": "Book the room", "owner": None, "due": None, "quote": "q", "source": "utt_3"}
        ]
        host = harness["build"](
            [self.plugin()], llm=ItemsLLM("book the ROOM"), store_artifacts=planted(existing)
        )
        assert await ran(host, "action_items", "sess_1", utterance_ids=["utt_3"]) is None
        assert harness["artifacts"] == []

    async def test_what_was_added_is_reported_separately_from_the_total(self, harness) -> None:
        """`count` alone cannot tell "found one" from "found none and there were
        none before" — which is exactly the confusion the UI has to avoid."""
        existing = [{"text": "Send the Q3 numbers", "owner": None, "due": None, "quote": "q"}]
        host = harness["build"](
            [self.plugin()], llm=ItemsLLM("Book the room"), store_artifacts=planted(existing)
        )
        await ran(host, "action_items", "sess_1", utterance_ids=["utt_3"])
        markdown = [a for _sid, _n, a in harness["artifacts"] if a.kind == "action_items"][-1]
        assert markdown.metadata == {"count": 2, "added": 1, "linked": 0}

    async def test_a_list_kept_only_as_markdown_is_still_added_to(self, harness) -> None:
        """With `also_emit_json` off there is no lossless record, and reading
        nothing back meant replacing a list the operator can see on screen with
        one built from a single line. So the rendered list is read back."""
        rendered = [
            {
                "kind": "action_items",
                "current": True,
                "content": (
                    "## Action items\n\n- [ ] Send the Q3 numbers — **Anna** · _by Friday_\n  > q"
                ),
            }
        ]
        host = harness["build"](
            [self.plugin()], llm=ItemsLLM("Book the room"), store_artifacts=rendered
        )
        artifact = await ran(host, "action_items", "sess_1", utterance_ids=["utt_3"])
        assert "Send the Q3 numbers" in artifact["content"]
        assert "**Anna**" in artifact["content"]
        assert "_by Friday_" in artifact["content"]
        assert "Book the room" in artifact["content"]

    async def test_a_whole_session_run_still_replaces(self, harness) -> None:
        """Re-running after a transcript edit must not accumulate the answers of
        every previous run (FR-SES-9)."""
        existing = [{"text": "Send the Q3 numbers", "owner": None, "due": None, "quote": "q"}]
        host = harness["build"](
            [self.plugin()], llm=ItemsLLM("Book the room"), store_artifacts=planted(existing)
        )
        artifact = await ran(host, "action_items", "sess_1")
        assert "Send the Q3 numbers" not in artifact["content"]

    async def test_a_picked_line_is_not_re_judged(self, harness) -> None:
        """The reason the button looked like it did nothing.

        The session-wide rules are written against an unattended pass over a
        whole transcript, where the failure mode is inventing commitments. Asked
        of a line somebody deliberately picked, they answer the wrong question —
        they weigh whether it counts, decide it is only a remark, and return
        nothing. So a scoped run is given its own instructions.
        """
        llm = ItemsLLM()
        host = harness["build"]([self.plugin()], llm=llm, store_artifacts=[])

        await ran(host, "action_items", "sess_1", utterance_ids=["utt_2"])
        assert "already made" in llm.systems[0]
        assert "Do not infer, suggest, or invent" not in llm.systems[0]

        await ran(host, "action_items", "sess_1")
        assert "Do not infer, suggest, or invent" in llm.systems[1]
        assert "already made" not in llm.systems[1]

    async def test_an_item_records_the_line_it_came_from(self, harness) -> None:
        """What lets the transcript show which lines are already tasks — and
        survive a reload, which remembering the click would not."""
        host = harness["build"]([self.plugin()], llm=ItemsLLM(), store_artifacts=[])
        await ran(host, "action_items", "sess_1", utterance_ids=["utt_2"])
        record = [a for _sid, _n, a in harness["artifacts"] if a.kind == "action_items_json"][-1]
        assert [item["source"] for item in json.loads(record.content)] == ["utt_2"]

    async def test_a_whole_session_item_belongs_to_no_line(self, harness) -> None:
        """Nobody picked those, so nothing in the transcript should be marked."""
        host = harness["build"]([self.plugin()], llm=ItemsLLM(), store_artifacts=[])
        await ran(host, "action_items", "sess_1")
        record = [a for _sid, _n, a in harness["artifacts"] if a.kind == "action_items_json"][-1]
        assert all("source" not in item for item in json.loads(record.content))

    async def test_no_previous_list_starts_one(self, harness) -> None:
        host = harness["build"]([self.plugin()], llm=ItemsLLM(), store_artifacts=[])
        artifact = await ran(host, "action_items", "sess_1", utterance_ids=["utt_2"])
        assert "Book the room" in artifact["content"]

    async def test_a_line_with_nothing_in_it_files_nothing(self, harness) -> None:
        """The reported bug. A line holding no commitment used to file an
        artifact saying "no action items were committed to in this session" —
        over the top of the session's real list, and reported to the operator
        as a successful add.
        """

        class Empty:
            available = True

            async def complete(self, prompt: str, **kwargs: object) -> str:
                return "[]"

        existing = [{"text": "Send the Q3 numbers", "owner": None, "due": None, "quote": "q"}]
        host = harness["build"]([self.plugin()], llm=Empty(), store_artifacts=planted(existing))
        assert await ran(host, "action_items", "sess_1", utterance_ids=["utt_1"]) is None
        assert harness["artifacts"] == []  # the list on screen is untouched

        # A whole-session run that finds nothing is a different statement, and
        # is still worth recording.
        whole = await ran(host, "action_items", "sess_1")
        assert "this session" in whole["content"]

    async def test_a_rewording_of_an_item_already_listed_is_not_added(self, harness) -> None:
        """The whole-session pass writes "Finish the migration"; a click on the
        line it came from writes "Finish the migration by Friday". A list
        holding both is worse than either, and exact-text matching lets both
        through — this was seen against the real model, not imagined."""
        existing = [{"text": "Finish the migration", "owner": None, "due": None, "quote": "q"}]
        host = harness["build"](
            [self.plugin()],
            llm=ItemsLLM("Finish the migration by Friday"),
            store_artifacts=planted(existing),
        )
        artifact = await ran(host, "action_items", "sess_1", utterance_ids=["utt_1"])

        # Not added — but the click did say which line it came from, which is
        # what marks that line in the transcript. Nothing added, one linked.
        assert artifact is not None
        assert artifact["metadata"] == {"count": 1, "added": 0, "linked": 1}
        assert artifact["content"].count("- [ ]") == 1

    async def test_a_genuinely_different_task_still_gets_through(self, harness) -> None:
        """The containment rule must not swallow everything: two tasks sharing a
        verb are still two tasks."""
        existing = [{"text": "Finish the migration", "owner": None, "due": None, "quote": "q"}]
        host = harness["build"](
            [self.plugin()],
            llm=ItemsLLM("Finish the security review"),
            store_artifacts=planted(existing),
        )
        artifact = await ran(host, "action_items", "sess_1", utterance_ids=["utt_1"])
        assert artifact is not None
        assert "security review" in artifact["content"]

    async def test_a_session_end_pass_does_not_delete_what_a_person_picked(self, harness) -> None:
        """The bug: an action item made *during* the recording vanished the
        moment the session stopped.

        Stopping dispatches `session.end`, the whole-session pass ran, and it
        replaced the list wholesale — including everything clicked line by line
        while the conversation was still going. A re-run must refresh what the
        machine found without deleting what a person chose.
        """
        picked = [
            {
                "text": "Book the room",
                "owner": None,
                "due": None,
                "quote": "q",
                "source": "utt_3",
            }
        ]
        host = harness["build"](
            [self.plugin()], llm=ItemsLLM("Send the Q3 numbers"), store_artifacts=planted(picked)
        )
        artifact = await ran(host, "action_items", "sess_1")

        assert "Book the room" in artifact["content"]  # the click survived
        assert "Send the Q3 numbers" in artifact["content"]  # …and the pass ran

    async def test_a_session_end_pass_still_drops_its_own_stale_answer(self, harness) -> None:
        """FR-SES-9: re-running after an edit must not accumulate every previous
        machine answer. Only the picked ones are carried forward."""
        stale = [{"text": "Something it no longer finds", "owner": None, "due": None, "quote": "q"}]
        host = harness["build"](
            [self.plugin()], llm=ItemsLLM("Send the Q3 numbers"), store_artifacts=planted(stale)
        )
        artifact = await ran(host, "action_items", "sess_1")
        assert "no longer finds" not in artifact["content"]


class TestOnDemandPlugins:
    """Some work is too expensive to do on the chance somebody wants it.

    Summarising costs a whole-transcript LLM call, and most recordings are never
    opened twice. Doing it automatically bills for summaries nobody asked for
    and — on a cloud credential — sends every conversation to a provider as a
    matter of course.
    """

    def summary(self):
        from droid_assistant.plugins.builtin.summary import SummaryPlugin

        return SummaryPlugin()

    async def test_an_on_demand_plugin_is_not_dispatched(self, harness) -> None:
        host = harness["build"]([self.summary(), GoodPlugin()])
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=5)
        finally:
            await host.stop()

        kinds = [artifact.kind for _sid, _name, artifact in harness["artifacts"]]
        assert "summary" not in kinds
        assert "good" in kinds  # …and the others still run

    async def test_it_still_runs_when_asked(self, harness) -> None:
        """`subscribes` says which handler an on-demand run reaches; only the
        dispatching is switched off."""
        host = harness["build"]([self.summary()])
        host.plugins["summary"].config = {"min_utterances": 1}  # the double has three lines
        artifact = await ran(host, "summary", "sess_1")
        assert artifact is not None
        assert artifact["kind"] == "summary"

    async def test_the_api_says_which_plugins_are_on_demand(self, harness) -> None:
        """The UI needs it to explain an empty tab as a choice rather than a
        failure."""
        host = harness["build"]([self.summary(), GoodPlugin()])
        listing = {row["name"]: row["on_demand"] for row in host.listing()}
        assert listing == {"summary": True, "good": False}

    async def test_declining_says_why(self, harness) -> None:
        """ "The plugin produced no artifact" is true and useless. Whoever
        pressed the button needs a reason they can act on."""

        class Picky(Plugin):
            name = "picky"
            version = "1.0.0"
            api_version = 1
            subscribes = {Event.SESSION_END}

            async def on_session_end(self, ctx: Context) -> Artifact | None:
                ctx.decline("nothing here worth summarising")
                return None

        host = harness["build"]([Picky()])
        artifact, reason = await host.run_now("picky", "sess_1")
        assert artifact is None
        assert reason == "nothing here worth summarising"

    async def test_an_explicit_request_is_not_refused_for_being_short(self, harness) -> None:
        """`min_utterances` stops a voice note being summarised on the way past.
        Somebody who pressed the button has already answered that question, and
        refusing them is a control that does nothing for no stated reason."""
        host = harness["build"]([self.summary()])
        host.plugins["summary"].config = {"min_utterances": 99}  # the double has three lines
        artifact, reason = await host.run_now("summary", "sess_1")
        assert artifact is not None, reason


class RecordingLLM:
    """Keeps what it was asked, so the assembled prompt can be asserted on."""

    available = True

    def __init__(self, reply: str = "a summary") -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete(self, prompt: str, **kwargs: object) -> str:
        self.prompts.append(prompt)
        self.systems.append(str(kwargs.get("system") or ""))
        return self.reply


class PromptReporter(Plugin):
    """Says which prompt it was handed, so the plumbing can be asserted."""

    name = "reporter"
    version = "1.0.0"
    api_version = 1
    subscribes = {Event.SESSION_END}

    async def on_session_end(self, ctx: Context) -> Artifact:
        return Artifact(kind="seen", content=ctx.prompt.name if ctx.prompt else "built-in")


class TestCustomPrompts:
    """FR-PLG-14. `style` offers bullets, prose, or minutes — which covers the
    common cases and none of the specific ones. A support call, a one-to-one and
    a design review want different summaries, and no enum of ours guesses them.
    """

    def summary(self):
        from droid_assistant.plugins.builtin.summary import SummaryPlugin

        return SummaryPlugin()

    def prompt(self) -> Prompt:
        return Prompt(
            id="prm_1",
            name="Customer call",
            instructions="Lead with what the customer asked for, then what we promised.",
        )

    async def test_a_saved_prompt_replaces_the_built_in_instructions(self, harness) -> None:
        """Not appended to them. Somebody who wrote their own ordering does not
        also want three bullet headings they did not ask for."""
        llm = RecordingLLM()
        host = harness["build"]([self.summary()], llm=llm)
        host.plugins["summary"].config = {"min_utterances": 1, "style": "bullets"}

        await ran(host, "summary", "sess_1", prompt=self.prompt())
        asked = llm.prompts[0]
        assert "what the customer asked for" in asked
        assert "Write short bullet points" not in asked
        # The output language goes with the style: a prompt asking for Russian
        # must not be contradicted a line later by our own "write in English".
        assert "regardless of the transcript's language" not in asked

    async def test_the_transcript_and_the_length_ceiling_still_go_with_it(self, harness) -> None:
        """The ceiling is not a style choice — it is what bounds the reply, and
        the bill for it — and a prompt with no transcript under it is useless."""
        llm = RecordingLLM()
        host = harness["build"]([self.summary()], llm=llm)
        host.plugins["summary"].config = {"min_utterances": 1, "max_words": 250}

        await ran(host, "summary", "sess_1", prompt=self.prompt())
        assert "Use at most 250 words" in llm.prompts[0]
        assert "я пришлю цифры до пятницы" in llm.prompts[0]

    async def test_the_guard_against_invention_is_not_replaceable(self, harness) -> None:
        """A custom prompt is a matter of taste. "Never invent a decision" is
        not, and a summary that fabricates one is worse than no summary."""
        llm = RecordingLLM()
        host = harness["build"]([self.summary()], llm=llm)
        host.plugins["summary"].config = {"min_utterances": 1}

        await ran(host, "summary", "sess_1", prompt=self.prompt())
        assert "Never invent a decision" in llm.systems[0]

    async def test_the_artifact_records_which_prompt_produced_it(self, harness) -> None:
        """By name, so the record still reads after the prompt is edited or
        deleted — and without a `style` that had nothing to do with it."""
        host = harness["build"]([self.summary()], llm=RecordingLLM())
        host.plugins["summary"].config = {"min_utterances": 1}

        artifact = await ran(host, "summary", "sess_1", prompt=self.prompt())
        assert artifact["metadata"]["prompt"] == "Customer call"
        assert "style" not in artifact["metadata"]

    async def test_without_one_the_built_in_instructions_stand(self, harness) -> None:
        """The default has to be unchanged by all of this: every caller that
        existed before prompts did sends no prompt at all."""
        llm = RecordingLLM()
        host = harness["build"]([self.summary()], llm=llm)
        host.plugins["summary"].config = {"min_utterances": 1, "style": "minutes"}

        artifact = await ran(host, "summary", "sess_1")
        assert "Write formal minutes" in llm.prompts[0]
        assert artifact["metadata"]["style"] == "minutes"
        assert "prompt" not in artifact["metadata"]

    async def test_a_prompt_reaches_a_plugin_that_accepts_one(self, harness) -> None:
        class Accepting(PromptReporter):
            name = "accepting"
            accepts_prompt = True

        host = harness["build"]([Accepting()])
        artifact = await ran(host, "accepting", "sess_1", prompt=self.prompt())
        assert artifact["content"] == "Customer call"

    async def test_a_plugin_that_does_not_accept_one_is_not_handed_it(self, harness) -> None:
        """The picker is only offered where it does something, so this should not
        arise — but a plugin quietly receiving a prompt it ignores would let a
        caller believe the choice had taken effect."""
        host = harness["build"]([PromptReporter()])
        artifact = await ran(host, "reporter", "sess_1", prompt=self.prompt())
        assert artifact["content"] == "built-in"

    async def test_a_dispatched_event_never_carries_a_prompt(self, harness) -> None:
        """A prompt is something a person chose for one run. Nothing about
        `session.end` arriving on its own says which one they would have picked."""

        class Accepting(PromptReporter):
            name = "accepting"
            accepts_prompt = True

        host = harness["build"]([Accepting()])
        await host.start(workers=1)
        try:
            await host.dispatch(EventType.SESSION_END, "sess_1", {})
            await host.drain(timeout=5)
        finally:
            await host.stop()
        assert [a.content for _s, _n, a in harness["artifacts"]] == ["built-in"]

    def test_the_listing_says_which_plugins_accept_a_prompt(self, harness) -> None:
        """What the UI shows the picker on. Beside a plugin that ignores the
        choice it would be a control that lies."""
        host = harness["build"]([self.summary(), GoodPlugin()])
        listing = {row["name"]: row["accepts_prompt"] for row in host.listing()}
        assert listing == {"summary": True, "good": False}
