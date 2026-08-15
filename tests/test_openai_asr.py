"""The OpenAI transcription backend.

There is no API key in CI, so these test everything up to and including the
request we would send and the response we would parse — the request shape, the
three response formats, the capability claims, and the error messages — with a
stubbed transport. What they cannot cover is whether OpenAI still answers that
shape; that is what `droid-assistant doctor` is for.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import numpy as np
import pytest

from droid_assistant.backends.asr.openai_asr import (
    MAX_CHUNK_MS,
    OpenAIASRBackend,
    _explain,
    _parse_transcription,
    _split,
)
from droid_assistant.config import ASRConfig, Settings
from droid_assistant.domain import SAMPLE_RATE, AudioBuffer, StreamConfig


def speech(duration_ms: int = 2000, start_ms: int = 0) -> AudioBuffer:
    count = int(SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(count) / SAMPLE_RATE
    return AudioBuffer((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), start_ms=start_ms)


class Recorder:
    """Captures the request instead of sending it."""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.request: httpx.Request | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(self.status, json=self.payload)

    def field(self, name: str) -> str | None:
        """Read a multipart form field out of the captured request body."""
        assert self.request is not None
        body = self.request.content.decode("utf-8", "replace")
        marker = f'name="{name}"'
        if marker not in body:
            return None
        after = body.split(marker, 1)[1]
        return after.split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    # The real class is captured once, before any patching: installing a second
    # recorder in the same test would otherwise wrap the first factory and send
    # the request to the wrong recorder.
    real_client = httpx.AsyncClient

    def install(payload: dict[str, Any], status: int = 200) -> Recorder:
        recorder = Recorder(payload, status)
        transport = httpx.MockTransport(recorder.handler)

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return recorder

    return install


def backend(**overrides: Any) -> OpenAIASRBackend:
    config = ASRConfig(openai={"model": overrides.pop("model", "gpt-4o-transcribe"), **overrides})
    return OpenAIASRBackend(config, "sk-test-key")


class TestCapabilities:
    def test_only_whisper_claims_word_timestamps(self) -> None:
        """FR-ASR-5 is a real capability, and claiming it falsely would put
        fabricated word timings behind click-to-seek."""
        assert backend(model="whisper-1").capabilities.word_timestamps is True
        assert backend(model="gpt-4o-transcribe").capabilities.word_timestamps is False
        assert backend(model="gpt-4o-mini-transcribe").capabilities.word_timestamps is False

    def test_streaming_is_offered_because_realtime_exists(self) -> None:
        # Live mode goes through the Realtime endpoint whatever the batch model
        # is, so the backend as a whole is streaming (FR-LAT-1).
        assert backend().capabilities.streaming is True

    def test_it_is_honest_about_leaving_the_machine(self) -> None:
        assert backend().capabilities.local is False

    def test_the_diarizing_model_is_recognised(self) -> None:
        assert backend(model="gpt-4o-transcribe-diarize").diarizes is True
        assert backend(model="gpt-4o-transcribe").diarizes is False

    async def test_a_missing_credential_names_the_variable(self) -> None:
        empty = OpenAIASRBackend(ASRConfig(), None)
        with pytest.raises(RuntimeError, match=r"\$OPENAI_API_KEY"):
            await empty.load()


class TestBatchRequest:
    async def test_it_posts_wav_to_the_transcriptions_endpoint(self, patched) -> None:
        recorder = patched({"text": "we need to finish the migration"})
        results = await backend().transcribe(speech(), StreamConfig(languages=["en"]))

        assert recorder.request is not None
        assert recorder.request.url.path.endswith("/audio/transcriptions")
        assert recorder.request.headers["authorization"] == "Bearer sk-test-key"
        assert b"RIFF" in recorder.request.content  # a real WAV, not raw PCM
        assert results[0].text == "we need to finish the migration"

    async def test_a_single_pinned_language_is_forwarded(self, patched) -> None:
        recorder = patched({"text": "hello"})
        await backend().transcribe(speech(), StreamConfig(languages=["ru"]))
        assert recorder.field("language") == "ru"

    async def test_several_languages_are_not_pinned(self, patched) -> None:
        """R13: forcing one of several would mistranscribe the others outright,
        so detection is left to the provider."""
        recorder = patched({"text": "hello"})
        await backend().transcribe(speech(), StreamConfig(languages=["ru", "en"]))
        assert recorder.field("language") is None

    async def test_vocabulary_becomes_a_prompt(self, patched) -> None:
        recorder = patched({"text": "hello"})
        await backend().transcribe(speech(), StreamConfig(vocabulary=["Arsenii", "Tailscale"]))
        assert "Arsenii" in (recorder.field("prompt") or "")

    async def test_the_standing_prompt_and_vocabulary_combine(self, patched) -> None:
        recorder = patched({"text": "hello"})
        await backend(prompt="A meeting about infrastructure").transcribe(
            speech(), StreamConfig(vocabulary=["Marko"])
        )
        prompt = recorder.field("prompt") or ""
        assert "infrastructure" in prompt
        assert "Marko" in prompt

    async def test_response_format_follows_the_model(self, patched) -> None:
        recorder = patched({"text": "x"})
        await backend(model="gpt-4o-transcribe").transcribe(speech(), StreamConfig())
        assert recorder.field("response_format") == "json"

        recorder = patched({"text": "x", "segments": []})
        await backend(model="whisper-1").transcribe(speech(), StreamConfig())
        assert recorder.field("response_format") == "verbose_json"

        recorder = patched({"text": "x"})
        await backend(model="gpt-4o-transcribe-diarize").transcribe(speech(), StreamConfig())
        assert recorder.field("response_format") == "diarized_json"

    async def test_empty_audio_makes_no_request(self, patched) -> None:
        recorder = patched({"text": "should not be reached"})
        assert await backend().transcribe(AudioBuffer.empty(), StreamConfig()) == []
        assert recorder.request is None


class TestResponseParsing:
    def test_plain_json_covers_the_whole_buffer(self) -> None:
        audio = speech(3000, start_ms=60_000)
        results = _parse_transcription({"text": " hello there "}, audio, StreamConfig())
        assert len(results) == 1
        assert results[0].text == "hello there"
        # FR-ASR-4: offsets are absolute session time, not clip-relative.
        assert results[0].start_ms == 60_000
        assert results[0].end_ms == 63_000

    def test_verbose_json_yields_segments_and_word_timings(self) -> None:
        payload = {
            "text": "we need to finish",
            "language": "en",
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "we need"},
                {"start": 1.0, "end": 2.0, "text": "to finish"},
            ],
            "words": [
                {"word": "we", "start": 0.0, "end": 0.4},
                {"word": "need", "start": 0.4, "end": 1.0},
                {"word": "to", "start": 1.0, "end": 1.4},
                {"word": "finish", "start": 1.4, "end": 2.0},
            ],
        }
        results = _parse_transcription(payload, speech(2000, start_ms=10_000), StreamConfig())
        assert [r.text for r in results] == ["we need", "to finish"]
        assert results[0].start_ms == 10_000
        # Words are attached to the segment whose span contains them.
        assert [w.w for w in results[0].words] == ["we", "need"]
        assert [w.w for w in results[1].words] == ["to", "finish"]

    def test_diarized_json_yields_dense_speaker_indices(self) -> None:
        """The provider's labels are opaque strings; the palette and the
        "Speaker N" labels are indexed by position (FR-UI-16)."""
        payload = {
            "segments": [
                {"start": 0.0, "end": 1.0, "text": "first", "speaker": "spk_b"},
                {"start": 1.0, "end": 2.0, "text": "second", "speaker": "spk_a"},
                {"start": 2.0, "end": 3.0, "text": "third", "speaker": "spk_b"},
            ]
        }
        results = _parse_transcription(payload, speech(3000), StreamConfig())
        assert [r.speaker for r in results] == [0, 1, 0]

    def test_an_unknown_shape_degrades_to_text(self) -> None:
        # A model that grows or drops a field should not break the backend.
        results = _parse_transcription(
            {"text": "still fine", "segments": "not a list"}, speech(), StreamConfig()
        )
        assert results[0].text == "still fine"

    def test_an_empty_response_yields_nothing(self) -> None:
        assert _parse_transcription({"text": "   "}, speech(), StreamConfig()) == []
        assert _parse_transcription({}, speech(), StreamConfig()) == []


class TestErrors:
    async def test_a_rejected_key_says_which_variable(self, patched) -> None:
        patched({"error": {"message": "Incorrect API key"}}, status=401)
        with pytest.raises(RuntimeError, match=r"\$OPENAI_API_KEY"):
            await backend().transcribe(speech(), StreamConfig())

    async def test_an_unknown_model_lists_the_valid_ones(self, patched) -> None:
        patched({"error": {"message": "model not found"}}, status=404)
        with pytest.raises(RuntimeError) as raised:
            await backend(model="gpt-9-transcribe").transcribe(speech(), StreamConfig())
        assert "whisper-1" in str(raised.value)

    def test_rate_limiting_says_the_session_continues(self) -> None:
        from droid_assistant.config import OpenAIASRConfig

        message = _explain(429, json.dumps({"error": {"message": "slow down"}}), OpenAIASRConfig())
        assert "session continues" in message

    def test_a_non_json_body_still_produces_a_message(self) -> None:
        from droid_assistant.config import OpenAIASRConfig

        assert "502" in _explain(502, "<html>bad gateway</html>", OpenAIASRConfig())


class TestChunking:
    def test_short_audio_is_one_piece(self) -> None:
        assert len(_split(speech(5000), MAX_CHUNK_MS)) == 1

    def test_a_long_session_is_cut_to_fit_the_upload_limit(self) -> None:
        """A Batch-mode session can exceed the 25 MB limit; being rejected at
        the end of a meeting is the worst possible moment to find out."""
        pieces = _split(speech(25 * 60 * 1000), MAX_CHUNK_MS)
        assert len(pieces) == 3
        # Offsets stay absolute and contiguous across the cut.
        assert pieces[0].start_ms == 0
        assert pieces[1].start_ms == pieces[0].end_ms
        assert pieces[-1].end_ms == 25 * 60 * 1000


class TestRegistry:
    def test_the_backend_is_selectable_by_configuration(self) -> None:
        """FR-ASR-1: changing backend in config changes the engine, no code."""
        from droid_assistant.backends import registry

        built = registry.build_asr(Settings(asr={"backend": "openai"}))
        assert built.capabilities.name.startswith("openai:")

    def test_local_only_refuses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """C-4: raw audio must not leave the machine unless explicitly enabled."""
        from droid_assistant.backends import registry

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        settings = Settings(asr={"backend": "openai"}, privacy={"local_only": True})
        report = registry.validate(settings, registry.build_asr(settings))
        assert not report.ok
        assert "local_only" in report.errors[0]

    def test_live_mode_is_permitted_with_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from droid_assistant.backends import registry
        from droid_assistant.domain import LatencyMode

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        settings = Settings(asr={"backend": "openai"})
        report = registry.validate(settings, registry.build_asr(settings), LatencyMode.LIVE)
        assert report.ok

    def test_an_unknown_backend_now_lists_openai(self) -> None:
        from droid_assistant.backends import registry

        with pytest.raises(registry.BackendError, match="openai"):
            registry.build_asr(Settings(asr={"backend": "nope"}))
