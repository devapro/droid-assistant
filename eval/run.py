"""The evaluation harness (NFR-EVAL-1 … NFR-EVAL-5).

This is the first code the implementation plan asks for and the last thing that
should be removed, because without it "did that change make transcription
better?" is unanswerable and every model decision becomes taste.

It is standalone: audio files in, a comparison table out. It depends on the
backends and nothing else in the server — no database, no API, no session.

    uv run droid-assistant eval
    uv run droid-assistant eval --backends faster_whisper,deepgram --language sr

Corpus layout (`eval/corpus/` by default):

    corpus/
    ├── en/
    │   ├── desk-01.wav          16 kHz mono, recorded through the browser client
    │   ├── desk-01.txt          reference transcript
    │   └── desk-01.json         optional metadata: {"condition": "desk", "speakers": 1}
    ├── ru/ …
    ├── sr/ …
    └── multi/
        ├── table-4spk.wav
        ├── table-4spk.txt
        └── table-4spk.rttm      reference diarization

NFR-EVAL-5 matters more than it looks: recordings must be captured **through the
browser client**, not a studio path, or the measurement describes a signal chain
the product does not have.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from droid_assistant.backends import registry
from droid_assistant.backends.asr.base import ASRBackend
from droid_assistant.config import Settings
from droid_assistant.domain import AudioBuffer, LatencyMode, StreamConfig
from droid_assistant.store.audio import read_pcm

from .der import DERResult, diarization_error_rate, segments_from_rttm
from .wer import WERResult, corpus_wer, word_error_rate

DEFAULT_CORPUS = Path(__file__).resolve().parent / "corpus"

#: Targets from SRS §4.2, which this harness exists to confirm or replace
#: (NFR-EVAL-4). They are printed beside measurements so a regression is visible
#: without looking anything up.
WER_TARGETS = {
    ("en", "desk"): 0.10, ("en", "table"): 0.16,
    ("ru", "desk"): 0.15, ("ru", "table"): 0.22,
    ("sr", "desk"): 0.25, ("sr", "table"): 0.35,
}  # fmt: skip
DER_TARGETS = {("small", "desk"): 0.15, ("small", "table"): 0.22,
               ("large", "desk"): 0.25, ("large", "table"): 0.35}  # fmt: skip


@dataclass(slots=True)
class Clip:
    audio: Path
    reference: str
    language: str
    condition: str  # "desk" | "table"
    speakers: int
    rttm: Path | None = None

    @property
    def name(self) -> str:
        return f"{self.language}/{self.audio.stem}"


@dataclass
class ClipResult:
    clip: Clip
    backend: str
    wer: WERResult | None = None
    der: DERResult | None = None
    seconds: float = 0.0
    audio_seconds: float = 0.0
    hypothesis: str = ""
    error: str | None = None

    @property
    def realtime_factor(self) -> float:
        return self.seconds / self.audio_seconds if self.audio_seconds else 0.0


@dataclass
class Report:
    results: list[ClipResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def by_backend(self) -> dict[str, list[ClipResult]]:
        out: dict[str, list[ClipResult]] = {}
        for result in self.results:
            out.setdefault(result.backend, []).append(result)
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "results": [
                {
                    "clip": result.clip.name,
                    "backend": result.backend,
                    "language": result.clip.language,
                    "condition": result.clip.condition,
                    "wer": result.wer.to_json() if result.wer else None,
                    "der": result.der.to_json() if result.der else None,
                    "realtime_factor": round(result.realtime_factor, 3),
                    "error": result.error,
                }
                for result in self.results
            ],
        }


def discover_clips(corpus: Path, language: str | None = None) -> list[Clip]:
    clips: list[Clip] = []
    if not corpus.is_dir():
        return clips
    for audio in sorted(corpus.rglob("*")):
        if audio.suffix.lower() not in {".wav", ".flac", ".mp3", ".m4a", ".opus", ".ogg"}:
            continue
        transcript = audio.with_suffix(".txt")
        if not transcript.exists():
            continue
        folder = audio.parent.name
        meta_path = audio.with_suffix(".json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        clip_language = meta.get("language", folder if folder != "multi" else "en")
        if language and clip_language != language:
            continue
        rttm = audio.with_suffix(".rttm")
        clips.append(
            Clip(
                audio=audio,
                reference=transcript.read_text(encoding="utf-8").strip(),
                language=clip_language,
                # A filename is the most reliable signal available, and the
                # metadata file overrides it when it is wrong.
                condition=meta.get("condition", "table" if "table" in audio.stem else "desk"),
                speakers=int(meta.get("speakers", 4 if "table" in audio.stem else 1)),
                rttm=rttm if rttm.exists() else None,
            )
        )
    return clips


async def evaluate_clip(
    clip: Clip, backend: ASRBackend, settings: Settings, name: str
) -> ClipResult:
    result = ClipResult(clip=clip, backend=name)
    try:
        samples = read_pcm(clip.audio)
    except Exception as exc:
        result.error = f"could not read audio: {exc}"
        return result

    audio = AudioBuffer(samples)
    result.audio_seconds = audio.duration_ms / 1000

    config = StreamConfig(
        languages=[clip.language],
        target_language="en",
        mode=LatencyMode.BATCH,
        serbian_script=settings.asr.serbian_script,
    )

    began = time.perf_counter()
    try:
        segments = await backend.transcribe(audio, config)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result
    result.seconds = time.perf_counter() - began

    result.hypothesis = " ".join(segment.text for segment in segments).strip()
    result.wer = word_error_rate(clip.reference, result.hypothesis, clip.language)

    if clip.rttm is not None and settings.diarization.enabled:
        diarizer = registry.build_diarization(settings)
        if diarizer is not None:
            try:
                await diarizer.load()
                hypothesis = await diarizer.diarize(audio)
                result.der = diarization_error_rate(segments_from_rttm(str(clip.rttm)), hypothesis)
            except Exception as exc:
                result.error = (result.error or "") + f" diarization: {exc}"
            finally:
                await diarizer.close()
    return result


#: Where each backend keeps its model name. They differ because a Whisper size,
#: a Deepgram model, and a GigaAM variant are not the same kind of thing.
_MODEL_FIELD = {
    "gigaam": ("gigaam", "model"),
    "deepgram": ("deepgram", "model"),
    "openai": ("openai", "model"),
}


def _backend_settings(settings: Settings, name: str) -> Settings:
    """A copy of the configuration pointing at one backend.

    `Settings` is frozen, so this is a rebuild rather than a mutation — which is
    also what stops one backend's evaluation leaking into the next.

    Accepts `backend` or `backend:model`, and routes the model to whichever
    field that backend actually reads.
    """
    data = settings.model_dump()
    backend, _, model = name.partition(":")
    data["asr"]["backend"] = backend
    # Per-language routing would silently override the backend under test.
    data["asr"]["by_language"] = {}
    if model:
        section = _MODEL_FIELD.get(backend)
        if section is None:
            data["asr"]["model"] = model
        else:
            data["asr"][section[0]][section[1]] = model
    return Settings(**data)


async def main(
    *,
    settings: Settings,
    corpus_dir: Path | None = None,
    backends: list[str] | None = None,
    language: str | None = None,
    json_out: Path | None = None,
) -> int:
    corpus = corpus_dir or DEFAULT_CORPUS
    clips = discover_clips(corpus, language)

    if not clips:
        print(f"No labelled clips found in {corpus}.\n")
        print(_corpus_help(corpus))
        return 1

    names = backends or [settings.asr.backend]
    report = Report()

    print(f"Corpus: {corpus}  ({len(clips)} clip(s), {len(names)} backend(s))\n")

    for name in names:
        try:
            backend_settings = _backend_settings(settings, name)
            backend = registry.build_asr(backend_settings)
            await backend.load()
        except Exception as exc:
            print(f"  {name}: unavailable — {exc}\n")
            continue

        print(f"  {name} …")
        for clip in clips:
            result = await evaluate_clip(clip, backend, backend_settings, name)
            report.results.append(result)
            _print_clip(result)
        await backend.close()
        print()

    _print_summary(report)

    if json_out:
        json_out.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
        print(f"\nWrote {json_out}")

    # Fail the run when any clip errored, so this is usable as a CI gate.
    return 1 if any(r.error for r in report.results) else 0


def _print_clip(result: ClipResult) -> None:
    if result.error:
        print(f"    {result.clip.name:<28} error: {result.error}")
        return
    wer = result.wer.wer if result.wer else 0.0
    target = WER_TARGETS.get((result.clip.language, result.clip.condition))
    verdict = "" if target is None else ("  ✓" if wer <= target else f"  ✗ target {target:.0%}")
    line = (
        f"    {result.clip.name:<28} WER {wer:6.1%}"
        f"  ({result.wer.substitutions if result.wer else 0}S "
        f"{result.wer.deletions if result.wer else 0}D "
        f"{result.wer.insertions if result.wer else 0}I)"
        f"  RTF {result.realtime_factor:5.2f}{verdict}"
    )
    print(line)
    if result.der is not None:
        print(
            f"      {'':<26} DER {result.der.der:6.1%}"
            f"  ({result.der.reference_speakers}→{result.der.hypothesis_speakers} speakers)"
        )


def _print_summary(report: Report) -> None:
    """The side-by-side comparison table NFR-EVAL-3 asks for."""
    grouped = report.by_backend()
    if not grouped:
        return

    languages = sorted({r.clip.language for r in report.results})
    header = (
        f"{'backend':<24}" + "".join(f"{lang.upper():>10}" for lang in languages) + f"{'RTF':>8}"
    )
    print("\n" + header)
    print("-" * len(header))

    for name, results in grouped.items():
        cells = []
        for lang in languages:
            pairs = [
                (r.clip.reference, r.hypothesis)
                for r in results
                if r.clip.language == lang and not r.error
            ]
            cells.append(f"{corpus_wer(pairs, lang).wer:>9.1%}" if pairs else f"{'—':>10}")
        usable = [r for r in results if not r.error and r.audio_seconds]
        rtf = sum(r.realtime_factor for r in usable) / len(usable) if usable else 0.0
        print(f"{name:<24}" + "".join(cells) + f"{rtf:>8.2f}")

    ders = [r for r in report.results if r.der is not None]
    if ders:
        print(f"\n{'backend':<24}{'DER':>10}{'speakers':>12}")
        print("-" * 46)
        for name, results in grouped.items():
            with_der = [r for r in results if r.der is not None]
            if not with_der:
                continue
            mean = sum(r.der.der for r in with_der if r.der) / len(with_der)
            counts = ", ".join(
                f"{r.der.reference_speakers}→{r.der.hypothesis_speakers}" for r in with_der if r.der
            )
            print(f"{name:<24}{mean:>9.1%}  {counts:>12}")

    print(
        "\nRTF is processing time ÷ audio duration; below 1.0 is faster than realtime.\n"
        "Targets are the SRS §4.2 figures, which are provisional and should be replaced\n"
        "by these measurements before release (NFR-EVAL-4)."
    )


def _corpus_help(corpus: Path) -> str:
    return f"""To build one (NFR-EVAL-2):

  1. Record at least 10 minutes per language **through the browser client**,
     not a studio path — the measurement has to reflect the real signal chain.
  2. Record two multi-speaker sessions: one desk condition (device within
     ~0.5 m) and one table condition (a phone ~1.5 m from four people).
  3. Lay them out under {corpus}:

       {corpus}/en/desk-01.wav      the audio
       {corpus}/en/desk-01.txt      what was actually said
       {corpus}/en/desk-01.json     {{"condition": "desk", "speakers": 1}}

     For diarization, add an RTTM beside the audio:

       {corpus}/multi/table-4spk.rttm

  4. Re-run this command.

Export a session you already recorded to start from a machine transcript:

    curl -s localhost:8000/api/sessions/<id>/export?format=txt > desk-01.txt

then correct it by hand. Correcting is much faster than transcribing, and the
corrected file is the reference."""


if __name__ == "__main__":
    from droid_assistant.config import load

    raise SystemExit(asyncio.run(main(settings=load())))
