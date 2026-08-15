"""`GET /api/models` and per-language routing over the API.

The user-visible behaviour under test: pick a different model for Russian than
for English, from the web UI, without restarting the server.
"""

from __future__ import annotations

from typing import Any

import pytest

from droid_assistant.api.services import Services
from droid_assistant.backends.asr import catalog
from droid_assistant.config import Settings


@pytest.fixture
def multilingual(data_dir: Any) -> Settings:
    return Settings(
        server={"data_dir": data_dir, "disconnect_grace_s": 1.0, "min_free_disk_mb": 0},
        capture={"languages": ["en", "ru"], "target_language": "en"},
        asr={"backend": "mock"},
        vad={"backend": "energy"},
        diarization={"backend": "mock"},
        translation={"backend": "identity"},
        audio={"codec": "wav"},
        plugins={"directory": data_dir / "plugins"},
    )


@pytest.fixture
async def multi_services(multilingual: Settings) -> Any:
    container = await Services.create(multilingual)
    await container.start()
    yield container
    await container.shutdown()


@pytest.fixture
def multi_client(multi_services: Services) -> Any:
    from fastapi.testclient import TestClient

    from droid_assistant.api.app import create_app

    app = create_app(multi_services.settings, services=multi_services)
    with TestClient(app) as client:
        yield client


class TestListing:
    def test_reports_state_and_routing(self, client: Any) -> None:
        body = client.get("/api/models").json()
        assert body["default"] == "mock:large-v3-turbo"
        assert body["languages"] == ["en"]
        # Nothing is downloaded in a temporary data directory, and the endpoint
        # must say so rather than implying the models are there.
        states = {row["id"]: row["state"] for row in body["models"]}
        assert states["faster_whisper:small"] == "absent"
        assert states["gigaam:v3-rnnt"] == "absent"

    def test_language_coverage_is_explicit_for_narrow_models(self, client: Any) -> None:
        rows = {row["id"]: row for row in client.get("/api/models").json()["models"]}
        assert rows["gigaam:v3-rnnt"]["languages"] == ["ru"]
        assert rows["faster_whisper:Sagicc/faster-whisper-large-v3-sr"]["languages"] == ["sr"]
        # `null` means unrestricted, which is how the client decides whether a
        # model may be offered for a given language at all.
        assert rows["openai:whisper-1"]["languages"] is None

    def test_the_serbian_deepgram_model_is_distinguished_from_the_older_one(
        self, client: Any
    ) -> None:
        """The reason the Deepgram rows carry a language list at all.

        nova-3 added Serbian; nova-2 has never had it. Reported as unrestricted,
        both would be offered for an `sr` session and one of them would fail at
        the provider, at the moment of recording.
        """
        rows = {row["id"]: row for row in client.get("/api/models").json()["models"]}
        assert "sr" not in (rows["deepgram:nova-2"]["languages"] or [])
        assert "sr" in (rows["deepgram:nova-3"]["languages"] or [])
        # Both still cover the languages nova-2 always had.
        for model in ("nova-2", "nova-3"):
            assert {"en", "ru"} <= set(rows[f"deepgram:{model}"]["languages"] or [])

    def test_unrouted_language_reports_the_default(self, multi_client: Any) -> None:
        routing = multi_client.get("/api/models").json()["routing"]
        assert routing["ru"] == {"id": "mock:large-v3-turbo", "explicit": False}


class TestRouting:
    def test_sets_a_model_for_one_language_only(self, multi_client: Any) -> None:
        response = multi_client.patch(
            "/api/config", json={"asr_by_language": {"ru": "gigaam:v3-ctc"}}
        )
        assert response.status_code == 200

        routing = multi_client.get("/api/models").json()["routing"]
        assert routing["ru"] == {"id": "gigaam:v3-ctc", "explicit": True}
        # English is untouched — routing one language must not move the others.
        assert routing["en"] == {"id": "mock:large-v3-turbo", "explicit": False}

    def test_merges_rather_than_replacing(self, multi_client: Any) -> None:
        """A UI editing one row must not have to send back the others."""
        multi_client.patch("/api/config", json={"asr_by_language": {"ru": "gigaam:v3-ctc"}})
        multi_client.patch("/api/config", json={"asr_by_language": {"en": "faster_whisper:small"}})
        routing = multi_client.get("/api/models").json()["routing"]
        assert routing["ru"]["id"] == "gigaam:v3-ctc"
        assert routing["en"]["id"] == "faster_whisper:small"

    def test_empty_string_clears_the_override(self, multi_client: Any) -> None:
        multi_client.patch("/api/config", json={"asr_by_language": {"ru": "gigaam:v3-ctc"}})
        multi_client.patch("/api/config", json={"asr_by_language": {"ru": ""}})
        routing = multi_client.get("/api/models").json()["routing"]
        assert routing["ru"] == {"id": "mock:large-v3-turbo", "explicit": False}

    def test_bare_model_name_keeps_the_backend(self, multi_client: Any) -> None:
        multi_client.patch("/api/config", json={"asr_by_language": {"ru": "small"}})
        assert multi_client.get("/api/models").json()["routing"]["ru"]["id"] == "mock:small"

    @pytest.mark.asyncio
    async def test_a_session_pinned_to_one_language_gets_its_model(
        self, multi_services: Services
    ) -> None:
        """The whole point: routing has to reach the pipeline, not just the config."""
        settings = multi_services.settings
        updated = Settings(
            **{
                **settings.model_dump(),
                "asr": {
                    **settings.model_dump()["asr"],
                    "by_language": {"ru": {"backend": "mock", "model": "russian-one"}},
                },
            }
        )
        await multi_services.reconfigure(updated)

        russian = await multi_services.asr_for("ru")
        assert russian is not multi_services.asr
        assert multi_services._asr_by_language[("mock", "russian-one")] is russian
        # English has no override, so it stays on the default backend object —
        # loading a second copy of the same weights would be pure waste.
        assert await multi_services.asr_for("en") is multi_services.asr
        # Several languages offered ⇒ nothing to route on, so the default stands.
        assert await multi_services.asr_for(None) is multi_services.asr


class TestDownloads:
    def test_cloud_backend_is_refused_with_a_reason(self, client: Any) -> None:
        response = client.post("/api/models/download", json={"id": "openai:whisper-1"})
        assert response.status_code == 422
        # FR-UI-9: errors name the component and say what to do about it.
        assert "no weights" in response.json()["error"]

    def test_present_model_short_circuits(self, client: Any, services: Services) -> None:
        spec = catalog.spec_for("gigaam", "v3-ctc")
        location = catalog.location(spec, services.settings.models_dir)
        assert location is not None
        location.mkdir(parents=True, exist_ok=True)
        (location / "tokens.txt").write_text("a 0\n")

        response = client.post("/api/models/download", json={"id": "gigaam:v3-ctc"})
        assert response.status_code == 200
        assert response.json() == {"id": "gigaam:v3-ctc", "state": "present"}

    def test_a_failed_download_is_reported_against_the_model(self, client: Any) -> None:
        """A download that fails must not read as "still going" forever."""
        response = client.post("/api/models/download", json={"id": "whisper_cpp:base"})
        assert response.status_code == 202

        rows = {row["id"]: row for row in client.get("/api/models").json()["models"]}
        row = rows["whisper_cpp:base"]
        assert row["state"] == "failed"
        assert row["error"] and "whisper.cpp" in row["error"]


class TestEviction:
    @pytest.mark.asyncio
    async def test_rerouting_releases_the_old_model(self, multi_services: Services) -> None:
        """Each cached backend holds a loaded model. Re-routing three times in an
        afternoon must not leave three of them resident."""
        settings = multi_services.settings

        def with_route(model: str) -> Settings:
            data = settings.model_dump()
            return Settings(
                **{
                    **data,
                    "asr": {**data["asr"], "by_language": {"ru": {"model": model}}},
                }
            )

        await multi_services.reconfigure(with_route("first"))
        await multi_services.asr_for("ru")
        assert ("mock", "first") in multi_services._asr_by_language

        await multi_services.reconfigure(with_route("second"))
        await multi_services.asr_for("ru")
        assert ("mock", "first") not in multi_services._asr_by_language
        assert ("mock", "second") in multi_services._asr_by_language
