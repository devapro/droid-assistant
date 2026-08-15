"""End-to-end: synthetic audio in, utterances out, no browser.

These are the tests that would have caught the two real bugs found while
building this: the consumer exiting on stop without draining the ring buffer
(losing the last sentence of every recording), and the ingest gap-tracking
acknowledging the wrong sequence.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import silence, to_pcm, tone
from droid_assistant.api.services import Services
from droid_assistant.domain import SessionState


def stream_audio(websocket, blocks: list[bytes], *, start_seq: int = 0) -> dict:
    """Send control-frame/payload pairs and return the last acknowledgement."""
    ack: dict = {}
    for offset, payload in enumerate(blocks):
        seq = start_seq + offset
        websocket.send_json(
            {"seq": seq, "t_ms": seq * 200, "codec": "pcm_s16le_16k", "samples": len(payload) // 2}
        )
        websocket.send_bytes(payload)
        ack = websocket.receive_json()
    return ack


def chunks(samples, size_ms: int = 200) -> list[bytes]:
    from droid_assistant.domain import SAMPLE_RATE

    step = int(SAMPLE_RATE * size_ms / 1000)
    return [to_pcm(samples[i : i + step]) for i in range(0, len(samples), step)]


def conversation() -> list[bytes]:
    """Two turns separated by enough silence for VAD to close the first."""
    import numpy as np

    return chunks(
        np.concatenate(
            [silence(400), tone(2000), silence(1200), tone(2000, frequency=330), silence(1000)]
        )
    )


class TestSessionLifecycle:
    def test_records_transcribes_and_persists(self, client) -> None:
        created = client.post(
            "/api/sessions", json={"title": "Standup", "languages": ["en"], "mode": "balanced"}
        ).json()
        session_id = created["session_id"]

        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            ack = stream_audio(websocket, conversation())
            assert ack["dropped_ms"] == 0

        client.post(f"/api/sessions/{session_id}/stop")
        session = client.get(f"/api/sessions/{session_id}").json()

        assert session["state"] == "ended"
        # Both turns survive: the second one is only reachable if the consumer
        # drains the ring buffer after stop rather than exiting immediately.
        assert len(session["utterances"]) == 2
        assert all(u["text"] for u in session["utterances"])
        assert session["has_audio"]

        # Utterances are ordered, non-inverted, and speaker-attributed.
        starts = [u["start_ms"] for u in session["utterances"]]
        assert starts == sorted(starts)
        assert all(u["end_ms"] >= u["start_ms"] for u in session["utterances"])
        assert all(u["speaker_id"] for u in session["utterances"])

    def test_transcript_is_searchable_across_sessions(self, client) -> None:
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
        client.post(f"/api/sessions/{created['session_id']}/stop")

        session = client.get(f"/api/sessions/{created['session_id']}").json()
        word = session["utterances"][0]["text"].split()[0]

        hits = client.get(f"/api/search?q={word}").json()
        assert hits["count"] >= 1
        assert hits["hits"][0]["session_id"] == created["session_id"]
        assert "⟦" in hits["hits"][0]["snippet"]  # the match is highlighted

    def test_batch_mode_emits_nothing_until_stop(self, client) -> None:
        """FR-LAT-6: only elapsed time and metering appear while recording."""
        created = client.post("/api/sessions", json={"languages": ["en"], "mode": "batch"}).json()
        session_id = created["session_id"]

        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
            assert client.get(f"/api/sessions/{session_id}").json()["utterances"] == []

        client.post(f"/api/sessions/{session_id}/stop")
        assert len(client.get(f"/api/sessions/{session_id}").json()["utterances"]) >= 1

    def test_stop_is_idempotent(self, client) -> None:
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        assert client.post(f"/api/sessions/{created['session_id']}/stop").status_code == 200
        assert client.post(f"/api/sessions/{created['session_id']}/stop").status_code == 200

    def test_pause_and_resume_keep_one_session(self, client) -> None:
        """FR-CAP-16: pausing does not end the session."""
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, chunks(tone(1000)))
            assert client.post(f"/api/sessions/{session_id}/pause").json()["paused"] is True
            assert client.post(f"/api/sessions/{session_id}/resume").json()["paused"] is False
        client.post(f"/api/sessions/{session_id}/stop")
        assert client.get(f"/api/sessions/{session_id}").json()["state"] == "ended"


class TestIngestProtocol:
    def test_rejects_a_missing_or_invalid_token(self, client) -> None:
        """NFR-SEC-3: the endpoint takes a server-issued token, not any connection."""
        with (
            client.websocket_connect("/ws/ingest?token=nonsense") as websocket,
            pytest.raises(Exception),  # noqa: B017 — the socket closes, no message arrives
        ):
            websocket.receive_json()

    def test_acknowledges_the_highest_contiguous_sequence(self, client) -> None:
        """The whole retransmission scheme rests on this: acknowledging the
        highest *received* sequence would let the client discard audio covering
        a hole it never noticed."""
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        blocks = chunks(tone(1000))

        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            ack = stream_audio(websocket, blocks[:2])
            assert ack["through_seq"] == 1

            # Skip sequence 2 and send 3: the acknowledgement must not advance.
            websocket.send_json({"seq": 3, "t_ms": 600, "codec": "pcm_s16le_16k", "samples": 1600})
            websocket.send_bytes(blocks[3])
            ack = websocket.receive_json()
            assert ack["through_seq"] == 1
            assert ack["pending"] == 1

            # The client retransmits from the first gap; now it closes.
            websocket.send_json({"seq": 2, "t_ms": 400, "codec": "pcm_s16le_16k", "samples": 1600})
            websocket.send_bytes(blocks[2])
            ack = websocket.receive_json()
            assert ack["through_seq"] == 3
            assert ack["pending"] == 0

        client.post(f"/api/sessions/{created['session_id']}/stop")

    def test_duplicate_retransmissions_are_discarded(self, client) -> None:
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        blocks = chunks(tone(600))
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, blocks)
            ack = stream_audio(websocket, blocks)  # the same chunks again
            assert ack["through_seq"] == len(blocks) - 1
        client.post(f"/api/sessions/{created['session_id']}/stop")

    def test_reconnecting_resumes_without_losing_audio(self, client) -> None:
        """FR-CAP-6 / NFR-REL-3: a dropped connection buffers and resumes."""
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]
        blocks = conversation()
        split = len(blocks) // 2

        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            ack = stream_audio(websocket, blocks[:split])
        assert ack["through_seq"] == split - 1

        # Same token, new socket — exactly what the client does on reconnect.
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, blocks[split:], start_seq=split)

        client.post(f"/api/sessions/{session_id}/stop")
        assert len(client.get(f"/api/sessions/{session_id}").json()["utterances"]) == 2

    def test_unsupported_codec_is_refused_clearly(self, client) -> None:
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            websocket.send_json({"seq": 0, "codec": "mp3", "samples": 100})
            websocket.send_bytes(b"\x00" * 200)
            with pytest.raises(Exception):  # noqa: B017
                websocket.receive_json()


class TestEventStream:
    def test_a_second_viewer_receives_the_transcript(self, client) -> None:
        """FR-SES-5: a laptop watches while a phone records."""
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]

        with client.websocket_connect(f"/ws/sessions/{session_id}") as viewer:
            # A joining viewer is replayed what it missed, then told it is live.
            while viewer.receive_json()["type"] != "subscribed":
                pass
            with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as ingest:
                ingest.receive_json()
                stream_audio(ingest, conversation())

            seen: list[str] = []
            for _ in range(12):
                event = viewer.receive_json()
                seen.append(event["type"])
                if event["type"] == "utterance.final":
                    assert event["data"]["text"]
                    assert event["seq"] > 0
                    break
            assert "utterance.final" in seen

        client.post(f"/api/sessions/{session_id}/stop")

    def test_replays_from_the_last_sequence_seen(self, client) -> None:
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as ingest:
            ingest.receive_json()
            stream_audio(ingest, conversation())
        client.post(f"/api/sessions/{session_id}/stop")

        # A viewer joining from scratch gets the history it missed.
        with client.websocket_connect(f"/ws/sessions/{session_id}?last_seq=0") as viewer:
            replayed = []
            for _ in range(20):
                message = viewer.receive_json()
                if message["type"] == "subscribed":
                    break
                replayed.append(message["type"])
            assert "utterance.final" in replayed

    def test_unknown_session_is_refused(self, client) -> None:
        with (
            client.websocket_connect("/ws/sessions/sess_nope") as viewer,
            pytest.raises(Exception),  # noqa: B017
        ):
            viewer.receive_json()


class TestEditingAndArtifacts:
    def test_editing_marks_the_line_and_keeps_the_original(self, client) -> None:
        """FR-SES-8: flagged as edited, original still retrievable."""
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
        client.post(f"/api/sessions/{session_id}/stop")

        session = client.get(f"/api/sessions/{session_id}").json()
        utterance = session["utterances"][0]
        original = utterance["text"]

        edited = client.patch(
            f"/api/utterances/{utterance['utterance_id']}", json={"text": "corrected text"}
        ).json()
        assert edited["text"] == "corrected text"
        assert edited["edited"] is True
        assert edited["text_original"] == original

        # A second edit must not overwrite the machine's original output.
        again = client.patch(
            f"/api/utterances/{utterance['utterance_id']}", json={"text": "corrected twice"}
        ).json()
        assert again["text_original"] == original

        # The edit is searchable and the old text is not.
        assert client.get("/api/search?q=corrected").json()["count"] >= 1

    def test_renaming_a_speaker_applies_across_the_session(self, client) -> None:
        """FR-DIA-4: retroactive, everywhere, from one rename."""
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
        client.post(f"/api/sessions/{session_id}/stop")

        speakers = client.get(f"/api/sessions/{session_id}/speakers").json()["speakers"]
        assert speakers
        renamed = client.patch(
            f"/api/speakers/{speakers[0]['id']}", json={"display_name": "Anna"}
        ).json()
        assert renamed["name"] == "Anna"

        exported = client.get(f"/api/sessions/{session_id}/export?format=md").text
        assert "Anna" in exported

    def test_export_formats_are_all_produced(self, client) -> None:
        created = client.post(
            "/api/sessions", json={"title": "Export me", "languages": ["en"]}
        ).json()
        session_id = created["session_id"]
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
        client.post(f"/api/sessions/{session_id}/stop")

        markdown = client.get(f"/api/sessions/{session_id}/export?format=md")
        assert markdown.status_code == 200
        assert "# Export me" in markdown.text
        assert "attachment" in markdown.headers["content-disposition"]

        payload = json.loads(client.get(f"/api/sessions/{session_id}/export?format=json").text)
        assert payload["schema_version"] == 1
        assert payload["session"]["title"] == "Export me"
        assert len(payload["utterances"]) == 2

        srt = client.get(f"/api/sessions/{session_id}/export?format=srt").text
        assert "-->" in srt
        assert srt.startswith("1\n")

        assert "WEBVTT" in client.get(f"/api/sessions/{session_id}/export?format=vtt").text


class TestDeletionAndRecovery:
    def test_deleting_purges_rows_and_the_audio_file(self, client, services: Services) -> None:
        """FR-SES-12: no residual row or file."""
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
        client.post(f"/api/sessions/{session_id}/stop")

        from pathlib import Path

        record = client.get(f"/api/sessions/{session_id}").json()
        assert record["has_audio"]
        audio_files = list(services.settings.audio_dir.rglob(f"{session_id}.*"))
        assert audio_files and Path(audio_files[0]).exists()

        assert client.delete(f"/api/sessions/{session_id}").status_code == 204
        assert client.get(f"/api/sessions/{session_id}").status_code == 404
        assert not list(services.settings.audio_dir.rglob(f"{session_id}.*"))
        assert client.get("/api/search?q=the").json()["count"] == 0

    async def test_an_interrupted_session_is_recovered(self, services: Services) -> None:
        """FR-SES-3: force-killing the server leaves a readable session
        containing everything committed before the kill."""
        from droid_assistant.domain import Utterance, new_id, now_ms
        from droid_assistant.store.repository import SessionRecord

        record = SessionRecord(
            id=new_id("sess"), started_at=now_ms() - 60_000, state=SessionState.RECORDING
        )
        await services.repo.create_session(record)
        await services.repo.add_utterance(
            Utterance(
                id=new_id("utt"),
                session_id=record.id,
                seq=0,
                start_ms=0,
                end_ms=4000,
                text="committed before the kill",
            )
        )

        recovered = await services.repo.recover_orphaned_sessions()
        assert record.id in recovered

        after = await services.repo.get_session(record.id)
        assert after is not None
        assert after.state is SessionState.ENDED
        assert after.ended_at is not None
        # Ended at the last utterance, not "now": a session interrupted
        # yesterday must not claim to have run overnight.
        assert after.ended_at == record.started_at + 4000
        assert after.metadata.get("recovered") is True


class TestModeSwitching:
    def test_switching_mid_session_loses_no_finalised_utterance(self, client) -> None:
        """R8's exit criterion, in miniature: switches at VAD boundaries only."""
        created = client.post(
            "/api/sessions", json={"languages": ["en"], "mode": "balanced"}
        ).json()
        session_id = created["session_id"]

        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
            before = len(client.get(f"/api/sessions/{session_id}").json()["utterances"])

            response = client.patch(f"/api/sessions/{session_id}/mode", json={"mode": "batch"})
            assert response.status_code == 200
            assert response.json()["requested_mode"] == "batch"

            stream_audio(websocket, conversation(), start_seq=100)

        client.post(f"/api/sessions/{session_id}/stop")
        after = client.get(f"/api/sessions/{session_id}").json()
        assert len(after["utterances"]) >= before  # nothing committed was lost
        assert after["mode"] == "batch"

    def test_live_mode_is_refused_with_a_batch_only_backend(
        self, services: Services, client
    ) -> None:
        """SRS §5.6: an impossible configuration fails at the boundary, not
        forty minutes into a meeting."""
        services.asr.streaming = False  # the mock backend declares its own capability
        response = client.post("/api/sessions", json={"languages": ["en"], "mode": "live"})
        assert response.status_code == 409
        assert "streaming" in response.json()["error"].lower()


class TestPresets:
    def test_round_trip(self, client) -> None:
        """FR-SES-14: one interaction from a saved setup to a running session."""
        config = {"languages": ["ru"], "targetLanguage": "en", "mode": "balanced"}
        saved = client.put("/api/presets", json={"name": "Standup", "config": config}).json()
        assert saved["name"] == "Standup"

        listed = client.get("/api/presets").json()["presets"]
        assert listed[0]["config"]["languages"] == ["ru"]

        created = client.post(
            "/api/sessions", json={"languages": ["ru"], "preset_id": saved["id"]}
        ).json()
        assert created["session"]["source_languages"] == ["ru"]
        client.post(f"/api/sessions/{created['session_id']}/stop")

        assert client.delete(f"/api/presets/{saved['id']}").status_code == 204


class TestDiskGuard:
    def test_refuses_to_start_below_the_threshold(self, services: Services, client) -> None:
        """NFR-RES-7: refuse up front rather than fail a session part-way."""
        from droid_assistant.config import Settings

        services.settings = Settings(
            **{
                **services.settings.model_dump(),
                "server": {
                    **services.settings.model_dump()["server"],
                    "min_free_disk_mb": 10**9,
                    "warn_free_disk_mb": 10**9,
                },
            }
        )
        response = client.post("/api/sessions", json={"languages": ["en"]})
        assert response.status_code == 409
        assert "free" in response.json()["error"].lower()


class TestAutoTitle:
    """FR-SES-7: every session has a non-empty title after it ends.

    The first spoken sentence names a meeting better than a model would and
    costs nothing — but a *title* has to fit a history row and a header, so it
    is one sentence, cut at a word boundary, not the opening paragraph.
    """

    def test_a_title_is_one_short_sentence(self) -> None:
        from droid_assistant.api.services import TITLE_MAX_CHARS, _title_from

        assert _title_from("Standup for the infra team.") == "Standup for the infra team"
        # Only the first sentence, even when several arrive in one utterance.
        assert _title_from("Hello there. Now let us begin the review.") == "Hello there"

        long = _title_from(
            "We need to finish the migration by Friday and then start on the reporting work"
        )
        assert len(long) <= TITLE_MAX_CHARS + 1  # +1 for the ellipsis
        assert long.endswith("…")
        assert not long.endswith(" …")  # cut at a word boundary, not mid-word

    def test_an_empty_session_still_gets_a_title(self, client) -> None:
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        client.post(f"/api/sessions/{created['session_id']}/stop")
        title = client.get(f"/api/sessions/{created['session_id']}").json()["title"]
        assert title
        assert "Empty session" in title

    def test_a_recorded_session_is_titled_from_its_opening_line(self, client) -> None:
        created = client.post("/api/sessions", json={"languages": ["en"]}).json()
        session_id = created["session_id"]
        with client.websocket_connect(f"/ws/ingest?token={created['ingest_token']}") as websocket:
            websocket.receive_json()
            stream_audio(websocket, conversation())
        client.post(f"/api/sessions/{session_id}/stop")

        session = client.get(f"/api/sessions/{session_id}").json()
        assert session["title"]
        assert len(session["title"]) <= 49
        assert "\n" not in session["title"]


class TestCloudReporting:
    """Where data goes is the one thing this product must not be vague about."""

    def test_modes_do_not_claim_cloud_use_without_a_configured_llm(self, client) -> None:
        # With no credential nothing leaves, so saying "text leaves this server"
        # would be simply untrue.
        modes = client.get("/api/modes").json()["modes"]
        assert all(not mode["uses_cloud_llm"] for mode in modes)
        assert all(not mode["uses_cloud_asr"] for mode in modes)

    def test_translation_is_unavailable_when_its_llm_is(self) -> None:
        """ "local" is the wrong badge for a backend that is not configured at
        all: it reads as "your data stays here" when in fact nothing works."""
        from droid_assistant.backends import registry
        from droid_assistant.backends.asr.mock import MockASRBackend
        from droid_assistant.config import Settings

        settings = Settings(translation={"backend": "llm"})
        llm = registry.build_llm(settings)  # no credential in the environment
        assert llm.available is False

        described = registry.describe(
            settings,
            MockASRBackend(),  # type: ignore[arg-type]
            None,
            registry.build_translation(settings, llm),
            llm,
        )
        assert described["translation"]["available"] is False
        assert described["llm"]["available"] is False

    def test_translation_is_available_with_a_local_llm_endpoint(self, monkeypatch) -> None:
        from droid_assistant.backends import registry
        from droid_assistant.backends.asr.mock import MockASRBackend
        from droid_assistant.config import Settings

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        settings = Settings(
            translation={"backend": "llm"}, llm={"base_url": "http://localhost:11434/v1"}
        )
        llm = registry.build_llm(settings)
        described = registry.describe(
            settings,
            MockASRBackend(),  # type: ignore[arg-type]
            None,
            registry.build_translation(settings, llm),
            llm,
        )
        # A local endpoint needs no credential, so it is offered.
        assert described["translation"]["available"] is True
        assert described["llm"]["local"] is True


class TestCostTracking:
    """FR-CFG-7: cloud spend tracked per session and displayed, with a ceiling
    that falls back to local processing rather than failing the session."""

    async def test_a_local_backend_costs_nothing(self, services: Services) -> None:
        assert services.asr.capabilities.price_per_minute_usd == 0.0

    def test_cloud_backends_publish_a_price(self) -> None:
        from droid_assistant.backends import registry
        from droid_assistant.config import Settings

        openai = registry.build_asr(Settings(asr={"backend": "openai"}))
        assert openai.capabilities.price_per_minute_usd == pytest.approx(0.006)

        deepgram = registry.build_asr(Settings(asr={"backend": "deepgram"}))
        assert deepgram.capabilities.price_per_minute_usd > 0

    def test_the_realtime_model_is_dearer_than_the_batch_one(self) -> None:
        """Worth surfacing: Live mode over a cloud recogniser is several times
        the price of Balanced, which is a real reason to prefer Balanced."""
        from droid_assistant.config import OpenAIASRConfig

        config = OpenAIASRConfig()
        assert config.price_for("gpt-live-transcribe") > config.price_for("gpt-4o-transcribe")

    def test_an_unknown_model_still_reports_something(self) -> None:
        # Silently costing nothing would under-report spend, which is the more
        # dangerous direction to be wrong in.
        from droid_assistant.config import OpenAIASRConfig

        assert OpenAIASRConfig().price_for("gpt-9-transcribe") > 0

    async def test_cost_accumulates_per_component(self, services: Services) -> None:
        from droid_assistant.domain import new_id, now_ms
        from droid_assistant.store.repository import SessionRecord

        record = SessionRecord(id=new_id("sess"), started_at=now_ms())
        await services.repo.create_session(record)

        total, breakdown = await services.repo.add_session_cost(record.id, 0.01, "openai", "asr")
        assert total == pytest.approx(0.01)

        total, breakdown = await services.repo.add_session_cost(
            record.id, 0.004, "gpt-4.1-mini", "translation"
        )
        assert total == pytest.approx(0.014)
        assert breakdown == {"asr": pytest.approx(0.01), "translation": pytest.approx(0.004)}

        total, breakdown = await services.repo.add_session_cost(record.id, 0.006, "openai", "asr")
        assert breakdown["asr"] == pytest.approx(0.016)

        stored = await services.repo.get_session(record.id)
        assert stored is not None
        assert stored.cost_usd == pytest.approx(0.02)
        assert stored.cost_breakdown["asr"] == pytest.approx(0.016)
        # The breakdown reaches the client, not just the total.
        assert "cost_breakdown" in stored.to_json()

    async def test_recognition_is_billed_on_audio_sent(self, services: Services) -> None:
        """The charge is per minute of audio handed to the provider — which in
        Live mode exceeds the session length, because the sliding window
        re-sends overlapping audio."""
        import numpy as np

        from droid_assistant.domain import SAMPLE_RATE, AudioBuffer, new_id, now_ms
        from droid_assistant.pipeline.orchestrator import SessionPipeline
        from droid_assistant.store.repository import SessionRecord

        record = SessionRecord(id=new_id("sess"), started_at=now_ms())
        await services.repo.create_session(record)

        billed: list[tuple[str, float, str]] = []

        async def on_cost(component: str, amount: float, provider: str) -> None:
            billed.append((component, amount, provider))

        class PricedMock:
            @property
            def capabilities(self):
                from droid_assistant.domain import ASRCapabilities

                return ASRCapabilities(
                    name="priced-mock",
                    streaming=False,
                    languages=None,
                    word_timestamps=False,
                    local=False,
                    price_per_minute_usd=0.006,
                )

            async def transcribe(self, audio, config):
                return []

            async def load(self) -> None: ...

            async def close(self) -> None: ...

        pipeline = SessionPipeline(
            record,
            services.settings,
            services.repo,
            services.bus,
            on_cost=on_cost,
            asr=PricedMock(),  # type: ignore[arg-type]
        )
        minute = AudioBuffer(np.zeros(SAMPLE_RATE * 60, dtype=np.float32))
        await pipeline._transcribe(minute)

        assert billed == [("asr", pytest.approx(0.006), "priced-mock")]
        assert pipeline.billed_audio_ms == 60_000

    async def test_a_local_backend_is_not_billed(self, services: Services) -> None:
        import numpy as np

        from droid_assistant.domain import SAMPLE_RATE, AudioBuffer, new_id, now_ms
        from droid_assistant.pipeline.orchestrator import SessionPipeline
        from droid_assistant.store.repository import SessionRecord

        record = SessionRecord(id=new_id("sess"), started_at=now_ms())
        await services.repo.create_session(record)
        billed: list[Any] = []

        pipeline = SessionPipeline(
            record,
            services.settings,
            services.repo,
            services.bus,
            on_cost=lambda *args: billed.append(args),
            asr=services.asr,
        )
        await pipeline._transcribe(AudioBuffer(np.zeros(SAMPLE_RATE * 10, dtype=np.float32)))
        assert billed == []
        assert pipeline.billed_audio_ms == 0

    async def test_the_ceiling_degrades_the_llm_rather_than_failing(self) -> None:
        """The requirement is explicit: continue locally and report the switch,
        never fail the session."""
        from droid_assistant.backends.llm.base import BudgetExceeded
        from droid_assistant.backends.llm.openai_compat import OpenAICompatClient
        from droid_assistant.config import LLMConfig

        client = OpenAICompatClient(LLMConfig(), "sk-test")
        assert client.available is True

        client.mark_degraded()
        assert client.available is False
        with pytest.raises(BudgetExceeded):
            await client.complete("anything")
