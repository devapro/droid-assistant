"""OpenAI transcription — the second cloud ASR backend (FR-ASR-3).

One backend, two endpoints, because OpenAI splits the job in two:

* **`/v1/audio/transcriptions`** takes a complete audio file. Balanced and Batch
  mode use it, one request per VAD-closed segment.
* **The Realtime WebSocket** takes a live PCM stream and emits deltas. Live mode
  uses it, and it is the only genuinely streaming path of the two.

Why this exists alongside Deepgram: it is the same credential the translation
and plugin features already need, so an operator who has set `OPENAI_API_KEY`
gets a cloud ASR option without a second account. It also covers the Tier D case
— a Raspberry Pi 4 cannot run Whisper usefully, so cloud ASR is the only way to
get usable Russian, let alone Serbian.

Model choice is a genuine trade, and `capabilities` reports it honestly rather
than claiming everything:

* `whisper-1` is the only model returning word timings, which is what
  click-a-word-to-seek needs (FR-ASR-5).
* `gpt-4o-transcribe-diarize` returns speaker labels of its own; the pipeline
  prefers them over its own clustering when present.
* the other `gpt-*-transcribe` models return text and nothing else — accurate,
  cheap, and timestamp-free.

Like every cloud backend this sends audio off the operator's machine, so it is
reachable only when policy allows it (C-4, FR-CFG-5).
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from ...config import (
    OPENAI_DIARIZING_MODELS,
    OPENAI_TIMESTAMP_MODELS,
    ASRConfig,
    OpenAIASRConfig,
)
from ...domain import ASRCapabilities, ASRResult, AudioBuffer, StreamConfig, Word
from .base import ASRBackend, ASRStream, decoding_prompt, postprocess

log = logging.getLogger(__name__)

#: The Realtime API speaks 24 kHz PCM; the pipeline speaks 16 kHz everywhere
#: else, so the streaming path resamples on the way out.
REALTIME_RATE = 24_000

#: `/v1/audio/transcriptions` accepts at most 25 MB, which at 16 kHz mono
#: 16-bit is a little over thirteen minutes. VAD segments are far shorter, but a
#: Batch-mode session is not, so it is chunked rather than rejected.
MAX_UPLOAD_BYTES = 25 * 1000 * 1000
MAX_CHUNK_MS = 10 * 60 * 1000


class OpenAIASRBackend(ASRBackend):
    def __init__(self, asr: ASRConfig, api_key: str | None) -> None:
        self._asr = asr
        self._config: OpenAIASRConfig = asr.openai
        self._api_key = api_key

    # --- capabilities -------------------------------------------------------

    @property
    def capabilities(self) -> ASRCapabilities:
        model = self._config.model
        return ASRCapabilities(
            name=f"openai:{model}",
            # Live mode goes through the Realtime endpoint, which is streaming
            # whatever the batch model is.
            streaming=True,
            languages=None,  # provider-side; validated by the API, not by us
            word_timestamps=model in OPENAI_TIMESTAMP_MODELS,
            local=False,
            confidence=False,
            price_per_minute_usd=self._config.price_for(model),
        )

    @property
    def diarizes(self) -> bool:
        """True when the model returns speaker labels the pipeline can use."""
        return self._config.model in OPENAI_DIARIZING_MODELS

    async def load(self) -> None:
        if not self._api_key:
            raise RuntimeError(
                f"asr.backend = 'openai' needs a key in ${self._config.api_key_env}. "
                "Set it in .env, or switch to a local backend with "
                "`asr.backend = 'faster_whisper'`."
            )

    # --- batch --------------------------------------------------------------

    def _response_format(self) -> str:
        model = self._config.model
        if model in OPENAI_TIMESTAMP_MODELS:
            return "verbose_json"  # the only format carrying timings
        if model in OPENAI_DIARIZING_MODELS:
            return "diarized_json"
        return "json"

    def _prompt(self, config: StreamConfig) -> str:
        """Steering text: the operator's standing prompt, then this session's
        vocabulary and the utterance being continued (FR-ASR-8)."""
        parts = [p for p in (self._config.prompt, decoding_prompt(config)) if p.strip()]
        return ". ".join(parts)

    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        if audio.samples.size == 0:
            return []
        results: list[ASRResult] = []
        for piece in _split(audio, MAX_CHUNK_MS):
            results.extend(await self._transcribe_one(piece, config))
        return postprocess(results, config, no_speech_threshold=self._asr.no_speech_threshold)

    async def _transcribe_one(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        import httpx

        from ...store.audio import wav_header

        pcm = audio.to_int16()
        body = wav_header(pcm.size) + pcm.tobytes()

        form: dict[str, Any] = {
            "model": self._config.model,
            "response_format": self._response_format(),
        }
        if pinned := config.pinned_language:
            form["language"] = pinned
        if prompt := self._prompt(config):
            form["prompt"] = prompt
        if self._config.model in OPENAI_TIMESTAMP_MODELS:
            form["timestamp_granularities[]"] = "word"

        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                f"{self._config.base_url.rstrip('/')}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=form,
                files={"file": ("audio.wav", body, "audio/wav")},
            )
        if response.status_code >= 400:
            raise RuntimeError(_explain(response.status_code, response.text, self._config))

        return _parse_transcription(response.json(), audio, config)

    # --- streaming ----------------------------------------------------------

    async def start_stream(self, config: StreamConfig) -> ASRStream:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "the openai backend needs the cloud extra: uv sync --extra cloud"
            ) from exc

        socket = await websockets.connect(
            self._config.realtime_url,
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            max_size=None,
            ping_interval=20,
        )
        session: dict[str, Any] = {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": REALTIME_RATE},
                    "transcription": {
                        "model": self._config.realtime_model,
                        "delay": self._config.realtime_delay,
                    },
                    # Our own Silero VAD already decides where turns end, and two
                    # voice detectors disagreeing is worse than either alone.
                    "turn_detection": None,
                }
            },
        }
        transcription = session["audio"]["input"]["transcription"]
        if config.languages:
            transcription["languages"] = list(config.languages)
        if prompt := self._prompt(config):
            transcription["prompt"] = prompt
        if config.vocabulary:
            transcription["keywords"] = list(config.vocabulary)

        await socket.send(json.dumps({"type": "session.update", "session": session}))
        return OpenAIRealtimeStream(socket, config, self._asr)


class OpenAIRealtimeStream:
    """One live transcription session over the Realtime WebSocket.

    Deltas are accumulated into a growing hypothesis and emitted as partials;
    the `completed` event replaces it with the final text. That is the same
    shape the rest of the pipeline already expects from a streaming backend.
    """

    def __init__(self, socket: Any, config: StreamConfig, asr: ASRConfig) -> None:
        self._socket = socket
        self._config = config
        self._asr = asr
        self._offset_ms = 0
        self._sent_ms = 0
        self._partial = ""
        self._closed = False

    async def push(self, audio: AudioBuffer) -> None:
        if self._closed or audio.samples.size == 0:
            return
        if self._sent_ms == 0:
            self._offset_ms = audio.start_ms
        pcm = _resample(audio.samples, audio.sample_rate, REALTIME_RATE)
        payload = base64.b64encode(pcm.tobytes()).decode("ascii")
        await self._socket.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))
        self._sent_ms += audio.duration_ms

    async def commit(self) -> None:
        """Close the current turn. Called on a VAD endpoint, since turn
        detection is delegated to our own VAD rather than the provider's."""
        if not self._closed:
            await self._socket.send(json.dumps({"type": "input_audio_buffer.commit"}))

    async def results(self) -> AsyncIterator[ASRResult]:
        async for raw in self._socket:
            if isinstance(raw, bytes):
                continue
            event = json.loads(raw)
            kind = event.get("type", "")

            if kind == "error":
                detail = event.get("error", {})
                raise RuntimeError(
                    "the OpenAI Realtime API rejected the session: "
                    f"{detail.get('message', detail)}. "
                    "Check asr.openai.realtime_model and asr.openai.realtime_url."
                )

            if kind.endswith("input_audio_transcription.delta"):
                self._partial += event.get("delta", "")
                if self._partial.strip():
                    yield self._as_result(self._partial, final=False)

            elif kind.endswith("input_audio_transcription.completed"):
                text = event.get("transcript", "") or self._partial
                self._partial = ""
                if text.strip():
                    for item in postprocess(
                        [self._as_result(text, final=True)],
                        self._config,
                        no_speech_threshold=self._asr.no_speech_threshold,
                    ):
                        yield item

    def _as_result(self, text: str, *, final: bool) -> ASRResult:
        return ASRResult(
            text=text,
            start_ms=self._offset_ms,
            end_ms=self._offset_ms + self._sent_ms,
            language=self._config.pinned_language,
            # The Realtime models return neither timings nor confidence, and
            # claiming otherwise would put fabricated numbers in the UI.
            confidence=None,
            words=[],
            no_speech_prob=None if final else 0.0,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._socket.close()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_transcription(
    payload: dict[str, Any], audio: AudioBuffer, config: StreamConfig
) -> list[ASRResult]:
    """Turn any of the three response shapes into `ASRResult`s.

    Deliberately tolerant: a model that grows a field should not break the
    backend, and one that drops a field should degrade to plain text rather
    than to an exception.
    """
    # diarized_json — segments carrying a speaker label.
    if isinstance(payload.get("segments"), list) and any(
        "speaker" in segment for segment in payload["segments"] if isinstance(segment, dict)
    ):
        speakers: dict[str, int] = {}
        results: list[ASRResult] = []
        for segment in payload["segments"]:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            label = str(segment.get("speaker", ""))
            index = speakers.setdefault(label, len(speakers))
            results.append(
                ASRResult(
                    text=text,
                    start_ms=audio.start_ms + int(float(segment.get("start", 0)) * 1000),
                    end_ms=audio.start_ms + int(float(segment.get("end", 0)) * 1000),
                    language=payload.get("language") or config.pinned_language,
                    speaker=index,
                )
            )
        if results:
            return results

    # verbose_json — segments plus optional word timings.
    if isinstance(payload.get("segments"), list) and payload["segments"]:
        words_by_time = [
            Word(
                w=str(word.get("word", "")).strip(),
                start_ms=audio.start_ms + int(float(word.get("start", 0)) * 1000),
                end_ms=audio.start_ms + int(float(word.get("end", 0)) * 1000),
            )
            for word in payload.get("words", [])
            if str(word.get("word", "")).strip()
        ]
        results = []
        for segment in payload["segments"]:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            start_ms = audio.start_ms + int(float(segment.get("start", 0)) * 1000)
            end_ms = audio.start_ms + int(float(segment.get("end", 0)) * 1000)
            results.append(
                ASRResult(
                    text=text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    language=payload.get("language") or config.pinned_language,
                    words=[w for w in words_by_time if start_ms <= w.start_ms < end_ms],
                    no_speech_prob=segment.get("no_speech_prob"),
                )
            )
        if results:
            return results

    # json / text — the whole buffer, no structure.
    text = str(payload.get("text", "")).strip()
    if not text:
        return []
    return [
        ASRResult(
            text=text,
            start_ms=audio.start_ms,
            end_ms=audio.end_ms,
            language=payload.get("language") or config.pinned_language,
        )
    ]


def _explain(status: int, body: str, config: OpenAIASRConfig) -> str:
    """NFR-REL-6: an actionable message, not a status code."""
    try:
        message = json.loads(body).get("error", {}).get("message", body)
    except Exception:
        message = body[:300]
    if status == 401:
        return (
            f"OpenAI rejected the credential in ${config.api_key_env}: {message}. "
            "Check the key, or switch to a local backend."
        )
    if status == 404:
        return (
            f"OpenAI does not recognise the model {config.model!r}: {message}. "
            "Set asr.openai.model to one of gpt-4o-transcribe, gpt-4o-mini-transcribe, "
            "gpt-4o-transcribe-diarize, or whisper-1."
        )
    if status == 429:
        return f"OpenAI rate-limited this request: {message}. The session continues; retry later."
    return f"OpenAI transcription failed ({status}): {message}"


def _split(audio: AudioBuffer, max_ms: int) -> list[AudioBuffer]:
    """Cut a long buffer into upload-sized pieces, keeping absolute offsets."""
    if audio.duration_ms <= max_ms and audio.samples.size * 2 <= MAX_UPLOAD_BYTES:
        return [audio]
    pieces: list[AudioBuffer] = []
    cursor = audio.start_ms
    while cursor < audio.end_ms:
        end = min(audio.end_ms, cursor + max_ms)
        pieces.append(audio.slice_ms(cursor, end))
        cursor = end
    return pieces


def _resample(samples: Any, src_rate: int, dst_rate: int) -> Any:
    from ...store.audio import resample_linear

    resampled = resample_linear(samples, src_rate, dst_rate)
    return np.clip(resampled * 32768.0, -32768, 32767).astype("<i2")


async def probe(config: OpenAIASRConfig, api_key: str | None, timeout: float = 5.0) -> bool:
    """Cheap reachability check for `/api/health` and `doctor`."""
    if not api_key:
        return False
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{config.base_url.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        return response.status_code < 500
    except Exception:
        return False


__all__ = ["OpenAIASRBackend", "OpenAIRealtimeStream", "probe"]
