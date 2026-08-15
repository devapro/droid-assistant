"""The model catalogue, and picking a model per language from the API.

These cover the thing that was previously impossible without editing
`config.toml` and restarting: knowing which weights are on disk, and routing one
language to a different engine than the rest (FR-ASR-1, FR-ASR-7, FR-CFG-8).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from droid_assistant.backends.asr import catalog
from droid_assistant.config import ConfigError, Settings, load


class TestParse:
    def test_backend_prefix_wins(self) -> None:
        assert catalog.parse("gigaam:v3-rnnt", "faster_whisper") == ("gigaam", "v3-rnnt")

    def test_bare_name_keeps_the_current_backend(self) -> None:
        assert catalog.parse("small", "faster_whisper") == ("faster_whisper", "small")

    def test_hugging_face_id_is_not_mistaken_for_a_backend(self) -> None:
        # `dvislobokov/faster-whisper-...` has no colon; a repo id that did
        # would still have the slash first, which is what disambiguates it.
        assert catalog.parse("dvislobokov/faster-whisper-large-v3-turbo-russian", "openai") == (
            "openai",
            "dvislobokov/faster-whisper-large-v3-turbo-russian",
        )

    def test_local_path_survives(self) -> None:
        backend, model = catalog.parse("/models/my-ct2-model", "faster_whisper")
        assert (backend, model) == ("faster_whisper", "/models/my-ct2-model")


class TestSpecs:
    def test_unknown_model_is_still_describable(self) -> None:
        """A custom fine-tune is a valid configuration; the UI must show it."""
        spec = catalog.spec_for("faster_whisper", "some-org/private-ct2")
        assert spec.id == "faster_whisper:some-org/private-ct2"
        assert spec.local is True
        assert spec.covers("ru")  # a Whisper conversion, so multilingual

    def test_gigaam_is_declared_russian_only(self) -> None:
        spec = catalog.spec_for("gigaam", "v3-rnnt")
        assert spec.covers("ru")
        assert not spec.covers("en")
        # This is the check that stops the UI offering it for English, where it
        # would return fluent nonsense rather than fail.
        assert spec.languages == frozenset({"ru"})

    def test_repo_map_matches_faster_whisper(self) -> None:
        """A drifted copy would report downloaded models as absent."""
        upstream = pytest.importorskip("faster_whisper.utils")
        for name, repo in catalog.WHISPER_REPOS.items():
            assert upstream._MODELS[name] == repo, f"{name} moved upstream"


class TestOnDisk:
    def test_absent_when_nothing_is_there(self, settings: Settings) -> None:
        spec = catalog.spec_for("faster_whisper", "small")
        assert catalog.state(spec, settings) == ("absent", spec.size_mb)

    def test_present_only_once_the_weights_are(self, settings: Settings) -> None:
        """An interrupted download leaves a directory. It must not read as ready."""
        spec = catalog.spec_for("faster_whisper", "small")
        location = catalog.location(spec, settings.models_dir)
        assert location is not None
        snapshot = location / "snapshots" / "abc123"
        snapshot.mkdir(parents=True)
        assert catalog.state(spec, settings)[0] == "absent"

        (snapshot / "model.bin").write_bytes(b"x" * 2_097_152)
        state, size = catalog.state(spec, settings)
        assert state == "present"
        assert size == 2

    def test_gigaam_presence_is_its_own_layout(self, settings: Settings) -> None:
        spec = catalog.spec_for("gigaam", "v3-ctc")
        location = catalog.location(spec, settings.models_dir)
        assert location is not None
        location.mkdir(parents=True)
        assert catalog.state(spec, settings)[0] == "absent"
        (location / "tokens.txt").write_text("a 0\n")
        assert catalog.state(spec, settings)[0] == "present"

    def test_symlinked_blobs_are_counted_once(self, settings: Settings) -> None:
        """The Hugging Face cache symlinks snapshots at blobs; following them
        reported every model at twice its real size."""
        spec = catalog.spec_for("faster_whisper", "small")
        location = catalog.location(spec, settings.models_dir)
        assert location is not None
        blob = location / "blobs" / "deadbeef"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"x" * 4_194_304)
        snapshot = location / "snapshots" / "abc123"
        snapshot.mkdir(parents=True)
        (snapshot / "model.bin").symlink_to(blob)
        assert catalog.state(spec, settings) == ("present", 4)

    def test_cloud_state_follows_the_credential(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = catalog.spec_for("openai", "gpt-4o-transcribe")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert catalog.state(spec, settings) == ("unavailable", 0)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert catalog.state(spec, settings) == ("ready", 0)


class TestRequired:
    """The download set is derived from the routing, never hand-listed."""

    def test_includes_every_routed_language(self, data_dir: Path) -> None:
        settings = Settings(
            server={"data_dir": data_dir},
            capture={"languages": ["en", "ru"], "target_language": "en"},
            asr={
                "backend": "faster_whisper",
                "model": "large-v3-turbo",
                "by_language": {"ru": {"backend": "gigaam", "model": "v3-rnnt"}},
            },
        )
        assert {spec.id for spec in catalog.required(settings)} == {
            "faster_whisper:large-v3-turbo",
            "gigaam:v3-rnnt",
        }

    def test_preload_adds_models_to_switch_to(self, data_dir: Path) -> None:
        settings = Settings(
            server={"data_dir": data_dir},
            capture={"languages": ["en"], "target_language": "en"},
            asr={"backend": "faster_whisper", "model": "small", "preload": ["gigaam:v3-ctc"]},
        )
        assert {spec.id for spec in catalog.required(settings)} == {
            "faster_whisper:small",
            "gigaam:v3-ctc",
        }

    def test_preload_accepts_a_comma_separated_string(self, data_dir: Path) -> None:
        """`.env` has no way to express a list, and JSON there is unpleasant."""
        settings = Settings(
            server={"data_dir": data_dir},
            asr={"preload": "faster_whisper:small, gigaam:v3-rnnt"},
        )
        assert settings.asr.preload == ["faster_whisper:small", "gigaam:v3-rnnt"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("faster_whisper:small,gigaam:v3-rnnt", ["faster_whisper:small", "gigaam:v3-rnnt"]),
            ('["gigaam:v3-ctc"]', ["gigaam:v3-ctc"]),  # the form every other list field takes
            ("", []),
            ("  a , b ,, c ", ["a", "b", "c"]),
        ],
    )
    def test_preload_from_the_environment(
        self,
        data_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
        expected: list[str],
    ) -> None:
        """Both forms have to survive the *env source*, not just the validator.

        pydantic-settings JSON-decodes list fields inside the environment source,
        before any validator runs — so the comma-separated form did not fall
        back to a split, it took the server down at startup with a parse error.
        Constructing `Settings(...)` directly does not exercise that path; only
        going through the environment does.
        """
        monkeypatch.setenv("DROID_DATA_DIR", str(data_dir))
        monkeypatch.setenv("DROID_ASR__PRELOAD", value)
        assert load().asr.preload == expected

    def test_malformed_json_names_both_accepted_forms(
        self, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DROID_DATA_DIR", str(data_dir))
        monkeypatch.setenv("DROID_ASR__PRELOAD", "[not json")
        with pytest.raises(ConfigError, match="comma-separated"):
            load()

    def test_cloud_backends_are_not_downloadable(self, data_dir: Path) -> None:
        settings = Settings(
            server={"data_dir": data_dir},
            capture={"languages": ["en"], "target_language": "en"},
            asr={"backend": "openai", "model": "gpt-4o-transcribe"},
        )
        assert catalog.required(settings) == []


class TestDownload:
    def test_refuses_a_cloud_backend_with_a_reason(self, settings: Settings) -> None:
        spec = catalog.spec_for("deepgram", "nova-2")
        with pytest.raises(ValueError, match="nothing to download"):
            catalog.download(spec, settings.models_dir)

    def test_already_present_returns_without_network(self, settings: Settings) -> None:
        spec = catalog.spec_for("gigaam", "v3-ctc")
        location = catalog.location(spec, settings.models_dir)
        assert location is not None
        location.mkdir(parents=True)
        (location / "tokens.txt").write_text("a 0\n")
        # No monkeypatching of urlopen: reaching the network here would fail the
        # offline test run, which is exactly the assertion.
        assert catalog.download(spec, settings.models_dir) == location
