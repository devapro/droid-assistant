"""Deepgram streaming ASR — the cloud backend (FR-ASR-3), and the answer for
Tier D hardware and for Live mode where local latency does not reach NFR-PERF-1.

This is the one backend that sends audio off the operator's machine, so it is
reachable only when `privacy.local_only` is false and the session has not opted
out (C-4, FR-CFG-5, FR-CFG-6). The UI shows the session as cloud-using, sourced
from `capabilities.local`.

Provider defaults live in config, so pointing this at Speechmatics or another
Deepgram-shaped endpoint is a config change (`asr.deepgram.endpoint`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from ...config import ASRConfig
from ...domain import SAMPLE_RATE, ASRCapabilities, ASRResult, AudioBuffer, StreamConfig, Word
from .base import ASRBackend, ASRStream, postprocess

log = logging.getLogger(__name__)


class DeepgramStream:
    """One live recognition socket.

    Deepgram is genuinely streaming, so unlike the LocalAgreement window over
    Whisper there is no hypothesis rewriting to manage here — interim results
    arrive flagged, and `is_final` marks the commit point.
    """

    def __init__(self, socket: Any, config: StreamConfig, asr_config: ASRConfig) -> None:
        self._socket = socket
        self._config = config
        self._asr_config = asr_config
        self._offset_ms = 0
        self._closed = False

    async def push(self, audio: AudioBuffer) -> None:
        if self._closed:
            return
        if self._offset_ms == 0:
            self._offset_ms = audio.start_ms
        await self._socket.send(audio.to_int16().tobytes())

    async def results(self) -> AsyncIterator[ASRResult]:
        async for raw in self._socket:
            if isinstance(raw, bytes):
                continue
            payload = json.loads(raw)
            if payload.get("type") == "Metadata":
                continue
            alternatives = payload.get("channel", {}).get("alternatives", [])
            if not alternatives or not alternatives[0].get("transcript"):
                continue
            alt = alternatives[0]
            start_ms = self._offset_ms + int(payload.get("start", 0) * 1000)
            end_ms = start_ms + int(payload.get("duration", 0) * 1000)
            words = [
                Word(
                    w=w.get("punctuated_word") or w["word"],
                    start_ms=self._offset_ms + int(w["start"] * 1000),
                    end_ms=self._offset_ms + int(w["end"] * 1000),
                )
                for w in alt.get("words", [])
            ]
            result = ASRResult(
                text=alt["transcript"],
                start_ms=start_ms,
                end_ms=end_ms,
                language=payload.get("channel", {}).get("detected_language")
                or (self._config.pinned_language),
                confidence=alt.get("confidence"),
                words=words,
            )
            cleaned = postprocess(
                [result], self._config, no_speech_threshold=self._asr_config.no_speech_threshold
            )
            for item in cleaned:
                # `speech_final` is Deepgram's endpoint marker; `is_final` alone
                # only means "this window will not be revised".
                yield item if payload.get("is_final") else _as_partial(item)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._socket.send(json.dumps({"type": "CloseStream"}))
            await self._socket.close()
        except Exception:
            pass


def _as_partial(result: ASRResult) -> ASRResult:
    return ASRResult(
        text=result.text,
        start_ms=result.start_ms,
        end_ms=result.end_ms,
        language=result.language,
        confidence=None,
        words=result.words,
    )


class DeepgramBackend(ASRBackend):
    def __init__(self, config: ASRConfig, api_key: str | None) -> None:
        self._config = config
        self._api_key = api_key

    @property
    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(
            name=f"deepgram:{self._config.deepgram.model}",
            streaming=True,
            languages=None,  # provider-side; validated by the API, not by us
            word_timestamps=True,
            local=False,
            confidence=True,
            price_per_minute_usd=self._config.deepgram.price_per_minute_usd,
        )

    async def load(self) -> None:
        if not self._api_key:
            raise RuntimeError(
                f"asr.backend = 'deepgram' needs a key in ${self._config.deepgram.api_key_env}. "
                "Set it in .env, or switch to a local backend with "
                "`asr.backend = 'faster_whisper'`."
            )

    def _url(self, config: StreamConfig, *, streaming: bool) -> str:
        params: dict[str, Any] = {
            "model": self._config.deepgram.model,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "punctuate": "true",
            "smart_format": "true",
            "interim_results": "true" if streaming else "false",
            "endpointing": 300,
        }
        pinned = config.pinned_language
        if pinned:
            params["language"] = pinned
        else:
            # Several pinned languages or none: let the provider detect. This is
            # the configuration R13 asks us to measure for code-switching.
            params["detect_language"] = "true"
        if config.vocabulary:
            params["keywords"] = config.vocabulary
        return f"{self._config.deepgram.endpoint}?{urlencode(params, doseq=True)}"

    async def start_stream(self, config: StreamConfig) -> ASRStream:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "the deepgram backend needs the cloud extra: uv sync --extra cloud"
            ) from exc
        socket = await websockets.connect(
            self._url(config, streaming=True),
            additional_headers={"Authorization": f"Token {self._api_key}"},
            max_size=None,
            ping_interval=5,
        )
        return DeepgramStream(socket, config, self._config)

    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        """Batch path — used by Balanced and Batch modes, and by the eval harness."""
        import httpx

        params = {
            "model": self._config.deepgram.model,
            "punctuate": "true",
            "smart_format": "true",
            "utterances": "true",
        }
        pinned = config.pinned_language
        params["language" if pinned else "detect_language"] = pinned or "true"

        rest_url = self._config.deepgram.endpoint.replace("wss://", "https://").replace(
            "/v1/listen", "/v1/listen"
        )
        body = _wav_bytes(audio)
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                rest_url,
                params=params,
                content=body,
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": "audio/wav",
                },
            )
            response.raise_for_status()
            payload = response.json()

        results: list[ASRResult] = []
        for utt in payload.get("results", {}).get("utterances", []):
            results.append(
                ASRResult(
                    text=utt.get("transcript", ""),
                    start_ms=audio.start_ms + int(utt.get("start", 0) * 1000),
                    end_ms=audio.start_ms + int(utt.get("end", 0) * 1000),
                    language=payload.get("results", {})
                    .get("channels", [{}])[0]
                    .get("detected_language", pinned),
                    confidence=utt.get("confidence"),
                    words=[
                        Word(
                            w=w.get("punctuated_word") or w["word"],
                            start_ms=audio.start_ms + int(w["start"] * 1000),
                            end_ms=audio.start_ms + int(w["end"] * 1000),
                        )
                        for w in utt.get("words", [])
                    ],
                )
            )
        if not results:  # no utterance segmentation: fall back to the flat channel
            channels = payload.get("results", {}).get("channels", [])
            if channels and channels[0].get("alternatives"):
                alt = channels[0]["alternatives"][0]
                if alt.get("transcript"):
                    results.append(
                        ASRResult(
                            text=alt["transcript"],
                            start_ms=audio.start_ms,
                            end_ms=audio.end_ms,
                            language=channels[0].get("detected_language", pinned),
                            confidence=alt.get("confidence"),
                        )
                    )
        return postprocess(results, config, no_speech_threshold=self._config.no_speech_threshold)


def _wav_bytes(audio: AudioBuffer) -> bytes:
    from ...store.audio import wav_header

    pcm = audio.to_int16()
    return wav_header(pcm.size) + pcm.tobytes()


async def probe(endpoint: str, api_key: str | None, timeout: float = 5.0) -> bool:
    """Cheap reachability check for `/api/health` and `doctor`."""
    if not api_key:
        return False
    import httpx

    url = endpoint.replace("wss://", "https://").rsplit("/v1/", 1)[0] + "/v1/projects"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers={"Authorization": f"Token {api_key}"})
        return response.status_code < 500
    except Exception:
        return False


__all__ = ["DeepgramBackend", "DeepgramStream", "probe"]
