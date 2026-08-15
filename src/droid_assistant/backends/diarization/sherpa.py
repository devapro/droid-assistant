"""`sherpa-onnx` offline diarization — the v1 default (SRS §6.2).

Chosen over the more accurate `pyannote` for installability: no PyTorch, no
Hugging Face token, no gated-model acceptance. For a project other people are
meant to install, that friction is a real cost, and `pyannote` remains available
as an opt-in backend for anyone who wants the accuracy.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np

from ...config import DiarizationConfig
from ...domain import SAMPLE_RATE, AudioBuffer, Embedding, SpeakerSegment
from ...pipeline.speakers import relabel_by_first_appearance
from .base import DiarizationBackend, DiarizationCapabilities, merge_adjacent

log = logging.getLogger(__name__)

MODEL_URLS = {
    "sherpa-onnx-pyannote-segmentation-3-0": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
        "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
    ),
    "nemo_en_titanet_small": (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
        "nemo_en_titanet_small.onnx"
    ),
}


class SherpaDiarizationBackend(DiarizationBackend):
    def __init__(self, config: DiarizationConfig, models_dir: Path) -> None:
        self._config = config
        self._models_dir = models_dir
        self._pipeline: Any = None
        self._extractor: Any = None
        self._make_config: Any = None
        # A pinned speaker count needs its own pipeline, and building one costs
        # a model load — so they are cached per count rather than per call.
        self._pinned: dict[int, Any] = {}
        self._lock = asyncio.Semaphore(1)

    @property
    def capabilities(self) -> DiarizationCapabilities:
        return DiarizationCapabilities(name="sherpa-onnx", embeddings=True, local=True)

    def _segmentation_path(self) -> Path:
        return self._models_dir / self._config.segmentation_model / "model.onnx"

    def _embedding_path(self) -> Path:
        return self._models_dir / f"{self._config.embedding_model}.onnx"

    async def load(self) -> None:
        if self._pipeline is not None:
            return
        try:
            import sherpa_onnx
        except ImportError as exc:
            # Two very different failures arrive as ImportError, and telling
            # someone to install a package they already have is worse than
            # saying nothing. A missing module is an install problem; a missing
            # `.dylib`/`.so` is a broken build of a package that *is* installed.
            if "sherpa_onnx" in str(exc) and "Library not loaded" not in str(exc):
                raise RuntimeError(
                    "diarization.backend = 'sherpa' needs: uv sync --extra local\n"
                    "or disable it with `diarization.enabled = false` (transcripts will be "
                    "unattributed)."
                ) from exc
            raise RuntimeError(
                f"sherpa-onnx is installed but its native library will not load:\n  {exc}\n\n"
                "This usually means the wheel was built from source without vendoring "
                "ONNX Runtime — it is a known problem on macOS arm64. Options:\n"
                "  • install a prebuilt wheel:\n"
                "      uv pip install sherpa-onnx -f https://k2-fsa.github.io/sherpa/onnx/cpu.html\n"
                "  • use the pyannote backend instead: diarization.backend = 'pyannote'\n"
                "  • run without speaker attribution: diarization.enabled = false"
            ) from exc

        seg, emb = self._segmentation_path(), self._embedding_path()
        for path, key in (
            (seg, self._config.segmentation_model),
            (emb, self._config.embedding_model),
        ):
            if not path.exists():
                raise RuntimeError(  # NFR-REL-6: model, path searched, command to fix
                    f"diarization model {key!r} not found at {path}.\n"
                    f"Fetch it with:  droid-assistant models download --diarization\n"
                    f"Source: {MODEL_URLS.get(key, 'see docs/INSTALL.md')}"
                )

        def make_config(num_clusters: int) -> Any:
            """`num_clusters = -1` infers the count; a positive value pins it."""
            return sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=str(seg)
                    ),
                ),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb)),
                clustering=sherpa_onnx.FastClusteringConfig(
                    num_clusters=num_clusters, threshold=self._config.clustering_threshold
                ),
                min_duration_on=0.3,
                min_duration_off=0.5,
            )

        def build() -> tuple[Any, Any]:
            pipeline = sherpa_onnx.OfflineSpeakerDiarization(make_config(-1))
            extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb))
            )
            return pipeline, extractor

        self._make_config = make_config
        self._pipeline, self._extractor = await asyncio.to_thread(build)
        log.info("diarization ready", extra={"backend": "sherpa-onnx"})

    async def close(self) -> None:
        self._pipeline = self._extractor = None
        self._pinned.clear()

    async def diarize(
        self,
        audio: AudioBuffer,
        *,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        if self._pipeline is None:
            await self.load()
        if audio.duration_ms < 500:
            return []

        lo = min_speakers if min_speakers is not None else self._config.min_speakers
        hi = max_speakers if max_speakers is not None else self._config.max_speakers

        def run() -> list[SpeakerSegment]:
            import sherpa_onnx

            pipeline = self._pipeline
            if lo is not None and hi is not None and lo == hi:
                # Ask the library for an exact count where it will honour it.
                # Some builds ignore `num_clusters` entirely, which is why
                # `_enforce_cap` below is not optional.
                if lo not in self._pinned:
                    self._pinned[lo] = sherpa_onnx.OfflineSpeakerDiarization(self._make_config(lo))
                pipeline = self._pinned[lo]
            result = pipeline.process(audio.samples).sort_by_start_time()
            return [
                SpeakerSegment(
                    start_ms=audio.start_ms + int(seg.start * 1000),
                    end_ms=audio.start_ms + int(seg.end * 1000),
                    speaker=int(seg.speaker),
                )
                for seg in result
            ]

        async with self._lock:
            segments = await asyncio.to_thread(run)

        if hi is not None:
            segments = await self._enforce_cap(segments, audio, hi)
        # sherpa's labels are neither dense nor reliably zero-based, and the
        # palette and the "Speaker N" labels are both indexed by them. Renumber
        # by first appearance so Speaker 1 is whoever spoke first (FR-UI-16).
        order = relabel_by_first_appearance([s.speaker for s in segments])
        segments = [SpeakerSegment(s.start_ms, s.end_ms, order[s.speaker]) for s in segments]
        return merge_adjacent(segments)

    async def _enforce_cap(
        self, segments: list[SpeakerSegment], audio: AudioBuffer, max_speakers: int
    ) -> list[SpeakerSegment]:
        """FR-DIA-3: never exceed a speaker count the user has told us.

        The library's own `num_clusters` is unreliable across builds, so the cap
        is applied here: cluster centroids are computed from the audio actually
        attributed to each label, and the two nearest are merged until the count
        fits. Agglomerative merging on cosine distance is the same criterion the
        clusterer used to split them, so this undoes over-splitting rather than
        making an unrelated choice.
        """
        labels = sorted({segment.speaker for segment in segments})
        if len(labels) <= max_speakers:
            return segments

        centroids: dict[int, Embedding] = {}
        for label in labels:
            owned = [s for s in segments if s.speaker == label]
            samples = np.concatenate([audio.slice_ms(s.start_ms, s.end_ms).samples for s in owned])
            vector = await self.embed(AudioBuffer(samples, start_ms=0))
            if vector is not None:
                centroids[label] = vector / (float(np.linalg.norm(vector)) or 1.0)

        merged_into = {label: label for label in labels}

        def resolve(label: int) -> int:
            while merged_into[label] != label:
                label = merged_into[label]
            return label

        remaining = list(centroids)
        while len(remaining) > max_speakers and len(remaining) > 1:
            best: tuple[float, int, int] | None = None
            for i, a in enumerate(remaining):
                for b in remaining[i + 1 :]:
                    similarity = float(np.dot(centroids[a], centroids[b]))
                    if best is None or similarity > best[0]:
                        best = (similarity, a, b)
            if best is None:
                break
            _, keep, drop = best
            merged_into[drop] = keep
            centroids[keep] = ((centroids[keep] + centroids[drop]) / 2).astype(np.float32)
            remaining.remove(drop)

        # Labels whose audio was too short to embed keep their own identity
        # unless they were explicitly merged.
        return [SpeakerSegment(s.start_ms, s.end_ms, resolve(s.speaker)) for s in segments]

    async def embed(self, audio: AudioBuffer) -> Embedding | None:
        if self._extractor is None:
            await self.load()
        if audio.duration_ms < 1000:
            # Below about a second the vector is not stable enough to cluster
            # on; FR-DIA-5 asks for ≥ 95% of utterances *longer than 1 s*.
            return None

        def run() -> Embedding | None:
            import numpy as np

            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=audio.samples)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                return None
            return np.asarray(self._extractor.compute(stream), dtype=np.float32)

        async with self._lock:
            return await asyncio.to_thread(run)
