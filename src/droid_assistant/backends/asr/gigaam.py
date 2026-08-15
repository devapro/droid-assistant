"""GigaAM — a Russian-specialised local ASR backend.

Whisper's multilingual capacity is weighted towards English, and the gap widens
sharply as the model shrinks. Measured on this project's own corpus, Russian
word error runs sixfold higher than English at `tiny` and is still measurably
worse at `large-v3-turbo`. GigaAM is trained for Russian specifically and closes
much of that.

It runs through `sherpa-onnx`, which this project already depends on for
diarization, so it costs no new runtime dependency and no PyTorch: the k2-fsa
project publishes GigaAM converted to ONNX with the feature extraction and
decoding handled inside the library.

Two decoders are published, and the trade is the usual one:

* **CTC** — smaller and faster, no language model over the output.
* **RNN-T (transducer)** — more accurate, larger, slower. The default here,
  because the reason to choose this backend at all is accuracy.

The `e2e` variants add punctuation and inverse text normalisation, which matters
for readability of a transcript nobody is going to re-punctuate by hand.

**Two things to know before choosing it.** It is Russian-only, so a session in
any other language must not be routed to it — see `asr.by_language`. And being
tuned for one language, it handles Russian–English code-switching *worse* than
Whisper: an English word inside a Russian sentence tends to come back
transliterated. If your speech mixes languages, measure both before switching.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ...config import GigaAMConfig
from ...domain import SAMPLE_RATE, ASRCapabilities, ASRResult, AudioBuffer, StreamConfig
from .base import ASRBackend, postprocess

log = logging.getLogger(__name__)

#: Published conversions. Each is a directory in the models tree.
MODELS = {
    "v3-rnnt": (
        "sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-nemo-transducer-giga-am-v3-russian-2025-12-16.tar.bz2",
    ),
    "v3-ctc": (
        "sherpa-onnx-nemo-ctc-giga-am-v3-russian-2025-12-16",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-nemo-ctc-giga-am-v3-russian-2025-12-16.tar.bz2",
    ),
    "v2-rnnt": (
        "sherpa-onnx-nemo-transducer-giga-am-v2-russian-2025-04-19",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-nemo-transducer-giga-am-v2-russian-2025-04-19.tar.bz2",
    ),
    "v2-ctc": (
        "sherpa-onnx-nemo-ctc-giga-am-v2-russian-2025-04-19",
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
        "sherpa-onnx-nemo-ctc-giga-am-v2-russian-2025-04-19.tar.bz2",
    ),
}

#: The only language these weights know. Routing anything else here produces
#: confident nonsense rather than an error, so the check is explicit.
LANGUAGE = "ru"


class GigaAMBackend(ASRBackend):
    def __init__(self, config: GigaAMConfig, models_dir: Path) -> None:
        self._config = config
        self._models_dir = models_dir
        self._recognizer: Any = None
        # sherpa's recognizers are not documented as thread-safe, and one
        # decode at a time is faster than two anyway.
        self._lock = asyncio.Semaphore(1)

    @property
    def capabilities(self) -> ASRCapabilities:
        return ASRCapabilities(
            name=f"gigaam:{self._config.model}",
            streaming=False,
            # Russian only. `registry.validate` refuses a session pinned to
            # anything else rather than letting it transcribe gibberish.
            languages=frozenset({LANGUAGE}),
            word_timestamps=False,
            local=True,
            confidence=False,
        )

    @property
    def model_dir(self) -> Path:
        directory, _url = MODELS.get(self._config.model, (self._config.model, ""))
        return self._models_dir / directory

    def _missing(self) -> RuntimeError:
        _directory, url = MODELS.get(self._config.model, ("", ""))
        return RuntimeError(  # NFR-REL-6: model, path searched, command to fix
            f"GigaAM model {self._config.model!r} not found at {self.model_dir}.\n"
            f"Fetch it with:  droid-assistant models gigaam {self._config.model}\n"
            + (f"Source: {url}" if url else "Set asr.gigaam.model to one of: " + ", ".join(MODELS))
        )

    async def load(self) -> None:
        if self._recognizer is not None:
            return
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError(
                "asr.backend = 'gigaam' needs sherpa-onnx: uv sync --extra local"
            ) from exc

        directory = self.model_dir
        tokens = directory / "tokens.txt"
        if not tokens.exists():
            raise self._missing()

        # Releases ship either `encoder.onnx` or `encoder.int8.onnx`, and
        # picking the wrong loader aborts inside the C++ library rather than
        # raising, so the file names are resolved rather than assumed.
        encoder = _resolve(directory, "encoder")
        transducer = encoder is not None

        def build() -> Any:
            common = {
                "tokens": str(tokens),
                "num_threads": self._config.num_threads,
                "sample_rate": SAMPLE_RATE,
                "feature_dim": self._config.feature_dim,
                "decoding_method": "greedy_search",
                "provider": self._config.provider,
            }
            if transducer:
                decoder = _resolve(directory, "decoder")
                joiner = _resolve(directory, "joiner")
                if decoder is None or joiner is None:
                    raise RuntimeError(
                        f"{directory} has an encoder but no matching decoder/joiner; "
                        "the download looks incomplete. Delete the directory and fetch it again."
                    )
                return sherpa_onnx.OfflineRecognizer.from_transducer(
                    encoder=str(encoder),
                    decoder=str(decoder),
                    joiner=str(joiner),
                    model_type="nemo_transducer",
                    **common,
                )
            model = _resolve(directory, "model")
            if model is None:
                raise RuntimeError(
                    f"no CTC model file found in {directory}. Delete the directory and fetch "
                    "it again."
                )
            return sherpa_onnx.OfflineRecognizer.from_nemo_ctc(model=str(model), **common)

        log.info(
            "loading GigaAM",
            extra={"model": self._config.model, "decoder": "rnnt" if transducer else "ctc"},
        )
        self._recognizer = await asyncio.to_thread(build)

    async def close(self) -> None:
        self._recognizer = None

    async def transcribe(self, audio: AudioBuffer, config: StreamConfig) -> list[ASRResult]:
        if self._recognizer is None:
            await self.load()
        if audio.samples.size == 0:
            return []

        def run() -> str:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio.samples)
            self._recognizer.decode_stream(stream)
            return str(stream.result.text).strip()

        async with self._lock:
            text = await asyncio.to_thread(run)
        if not text:
            return []

        # One segment per call: the VAD has already decided where this utterance
        # begins and ends, and sherpa returns a single transcript for it.
        return postprocess(
            [
                ASRResult(
                    text=text,
                    start_ms=audio.start_ms,
                    end_ms=audio.end_ms,
                    language=LANGUAGE,
                )
            ],
            config,
            no_speech_threshold=1.0,  # no per-segment no-speech score to threshold on
        )


def _resolve(directory: Path, stem: str) -> Path | None:
    """Find `<stem>.onnx`, or the quantised `<stem>.int8.onnx` some releases ship.

    Returns None rather than raising: the caller uses the encoder's presence to
    decide which decoder to build, and "absent" is a meaningful answer there.
    """
    exact = directory / f"{stem}.onnx"
    if exact.exists():
        return exact
    quantised = sorted(directory.glob(f"{stem}*.onnx"))
    return quantised[0] if quantised else None
