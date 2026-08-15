"""Configuration (FR-CFG-1 … FR-CFG-5).

The requirement that matters at three in the morning is FR-CFG-2: an invalid
value fails startup naming the field, the value, and what it accepts. These
tests assert on the *message*, not just that an exception was raised.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from droid_assistant.config import ConfigError, Settings, load


class TestSources:
    def test_defaults_are_a_working_configuration(self, tmp_path: Path) -> None:
        """A missing config file is not an error — the defaults must run."""
        settings = load(tmp_path / "absent.toml")
        assert settings.asr.backend == "faster_whisper"
        assert settings.capture.default_mode == "balanced"

    def test_toml_is_read(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text(
            "[asr]\nbackend = 'deepgram'\nmodel = 'nova-3'\n\n[server]\nport = 9000\n",
            encoding="utf-8",
        )
        settings = load(config)
        assert settings.asr.backend == "deepgram"
        assert settings.server.port == 9000

    def test_environment_overrides_the_file(self, tmp_path: Path, monkeypatch) -> None:
        """FR-CFG-1: both work, and environment takes precedence — which is
        what makes a container image configurable without rebuilding it."""
        config = tmp_path / "config.toml"
        config.write_text("[server]\nport = 9000\n", encoding="utf-8")
        monkeypatch.setenv("DROID_SERVER__PORT", "7777")
        assert load(config).server.port == 7777

    def test_data_dir_comes_from_the_environment(self, tmp_path: Path, monkeypatch) -> None:
        """SRS §5.9: pointing DROID_DATA_DIR at a USB SSD is the documented
        setup on Pi-class hardware."""
        monkeypatch.setenv("DROID_DATA_DIR", str(tmp_path / "ssd"))
        settings = load(tmp_path / "absent.toml")
        assert settings.server.data_dir == tmp_path / "ssd"
        assert settings.db_path == tmp_path / "ssd" / "droid.db"
        assert settings.audio_dir == tmp_path / "ssd" / "audio"


class TestValidation:
    def test_an_out_of_range_value_names_the_field(self) -> None:
        with pytest.raises(ConfigError) as raised:
            load(None, server={"port": 99999})
        message = str(raised.value)
        assert "server.port" in message
        assert "99999" in message

    def test_an_unknown_key_is_refused(self) -> None:
        """A typo in a config file should fail loudly, not be silently ignored
        while the operator wonders why the setting has no effect."""
        with pytest.raises(ConfigError) as raised:
            load(None, asr={"backned": "whisper"})
        assert "backned" in str(raised.value)

    def test_a_target_language_outside_the_list_is_refused_with_a_fix(self) -> None:
        with pytest.raises(ConfigError) as raised:
            load(None, capture={"languages": ["en"], "target_language": "de"})
        message = str(raised.value)
        assert "target_language" in message
        assert "add it" in message.lower()

    def test_an_empty_language_list_is_refused(self) -> None:
        with pytest.raises(ConfigError) as raised:
            load(None, capture={"languages": []})
        assert "at least one" in str(raised.value)

    def test_disk_thresholds_must_be_ordered(self) -> None:
        """A warning that fires after sessions are already refused is useless."""
        with pytest.raises(ConfigError) as raised:
            load(None, server={"min_free_disk_mb": 8000, "warn_free_disk_mb": 1000})
        assert "warn_free_disk_mb" in str(raised.value)

    def test_speaker_range_must_be_ordered(self) -> None:
        with pytest.raises(ConfigError) as raised:
            load(None, diarization={"min_speakers": 5, "max_speakers": 2})
        assert "min_speakers" in str(raised.value)


class TestSecrets:
    def test_credentials_are_never_stored_in_config(self) -> None:
        """FR-CFG-4 by construction: config holds the *name* of the variable, so
        no key material can reach a log line or an API response."""
        settings = Settings()
        dumped = settings.model_dump()
        assert dumped["llm"]["api_key_env"] == "OPENAI_API_KEY"
        assert not any("sk-" in str(value) for value in dumped.values())

    def test_redacted_output_reports_presence_not_value(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-here")
        redacted = Settings().redacted()
        assert redacted["llm"]["credential_present"] is True
        assert "sk-secret-value-here" not in str(redacted)

    def test_missing_credential_reports_absent(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert Settings().redacted()["llm"]["credential_present"] is False


class TestLocalOnly:
    def test_local_base_url_is_recognised(self) -> None:
        for url in (
            "http://localhost:11434/v1",
            "http://127.0.0.1:8080/v1",
            "http://host.docker.internal:1234/v1",
        ):
            assert Settings(llm={"base_url": url}).llm.is_local, url

    def test_remote_base_url_is_not_local(self) -> None:
        assert not Settings(llm={"base_url": "https://api.openai.com/v1"}).llm.is_local


class TestBackendValidation:
    def test_live_mode_with_a_batch_backend_is_an_error(self) -> None:
        """SRS §5.6: `capabilities` is what turns this from a forty-minute
        surprise into a startup error."""
        from droid_assistant.backends import registry
        from droid_assistant.backends.asr.mock import MockASRBackend
        from droid_assistant.domain import LatencyMode

        backend = MockASRBackend(streaming=False)
        report = registry.validate(Settings(), backend, LatencyMode.LIVE)  # type: ignore[arg-type]
        assert not report.ok
        assert "streaming" in report.errors[0]

    def test_local_only_with_a_cloud_backend_is_an_error(self) -> None:
        """C-4: raw audio must never leave the server unless explicitly enabled."""
        from droid_assistant.backends import registry
        from droid_assistant.config import Settings as S

        settings = S(privacy={"local_only": True}, asr={"backend": "deepgram"})
        backend = registry.build_asr(settings)
        report = registry.validate(settings, backend)
        assert not report.ok
        assert "local_only" in report.errors[0]

    def test_a_cloud_backend_warns_that_audio_leaves(self) -> None:
        from droid_assistant.backends import registry
        from droid_assistant.config import Settings as S

        settings = S(asr={"backend": "deepgram"})
        report = registry.validate(settings, registry.build_asr(settings))
        assert report.ok  # allowed, but stated
        assert any("leaves this server" in warning for warning in report.warnings)

    def test_several_languages_warn_about_code_switching(self) -> None:
        """R13, surfaced once at startup rather than discovered mid-meeting."""
        from droid_assistant.backends import registry
        from droid_assistant.backends.asr.mock import MockASRBackend

        settings = Settings(capture={"languages": ["ru", "en"], "target_language": "en"})
        report = registry.validate(settings, MockASRBackend())  # type: ignore[arg-type]
        assert any("one language per window" in warning for warning in report.warnings)

    def test_an_unknown_backend_name_lists_the_valid_ones(self) -> None:
        from droid_assistant.backends import registry

        with pytest.raises(registry.BackendError) as raised:
            registry.build_asr(Settings(asr={"backend": "wishper"}))
        assert "faster_whisper" in str(raised.value)


class TestLogging:
    def test_secrets_are_redacted_from_log_lines(self) -> None:
        """NFR-SEC-5, as a second line of defence for strings assembled outside
        the config layer — a backend URL carrying a key, for instance."""
        from droid_assistant.logging import redact

        assert "sk-abcdefghijklmnop" not in redact("using key sk-abcdefghijklmnop for the call")
        assert "***" in redact("api_key=supersecretvalue")
        assert "***" in redact("https://api.example.com/v1?token=abc123def456")

    def test_ordinary_text_is_untouched(self) -> None:
        from droid_assistant.logging import redact

        message = "transcribed 42 utterances in 3.1 s"
        assert redact(message) == message


def test_settings_are_frozen() -> None:
    """Immutability is what makes hot-swapping safe: a running session keeps the
    configuration it started with (FR-CFG-8)."""
    settings = Settings()
    with pytest.raises(Exception):  # noqa: B017 — pydantic raises its own type
        settings.server = None  # type: ignore[misc]


def test_models_dir_can_be_relocated(tmp_path: Path, monkeypatch) -> None:
    """Weights are regenerable and large; a shared or read-only model directory
    is a normal deployment (SRS §5.9 excludes them from backups)."""
    monkeypatch.setenv("DROID_MODELS_DIR", str(tmp_path / "shared-models"))
    assert Settings().models_dir == tmp_path / "shared-models"
    os.environ.pop("DROID_MODELS_DIR")
