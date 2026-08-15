"""Session audio: append-as-it-arrives, encode on stop, serve with byte ranges.

Audio is appended to a raw `.pcm` file while the session runs, so a kill at
minute 40 leaves 40 minutes of recoverable audio rather than a truncated
container. On stop it is transcoded once to the configured codec (FR-SIG-3).

`ffmpeg` is optional. Without it the session yields a WAV file — larger, still
playable, and the operator is told once rather than losing the recording.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import struct
import wave
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..domain import SAMPLE_RATE, Samples

log = logging.getLogger(__name__)

_CHUNK = 256 * 1024


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def session_audio_dir(root: Path, started_at_ms: int) -> Path:
    """`audio/YYYY/MM/DD/` — the SRS §5.9 layout, in UTC so the tree does not
    reorder itself when the operator travels."""
    stamp = datetime.fromtimestamp(started_at_ms / 1000, tz=UTC)
    return root / f"{stamp:%Y/%m/%d}"


class SessionAudioWriter:
    """Append-only PCM sink for one session."""

    def __init__(
        self,
        session_id: str,
        audio_root: Path,
        started_at_ms: int,
        *,
        codec: str = "opus",
        bitrate_kbps: int = 32,
    ) -> None:
        self.session_id = session_id
        self.codec = codec
        self.bitrate_kbps = bitrate_kbps
        self._dir = session_audio_dir(audio_root, started_at_ms)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._raw_path = self._dir / f"{session_id}.pcm"
        self._handle = self._raw_path.open("ab")
        self._samples_written = 0
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def duration_ms(self) -> int:
        return int(self._samples_written * 1000 / SAMPLE_RATE)

    @property
    def raw_path(self) -> Path:
        """The append-only PCM file, readable while the session is still running.

        Batch mode reads it on stop rather than keeping the session in RAM.
        """
        return self._raw_path

    async def append(self, samples: Samples) -> None:
        if self._closed or samples.size == 0:
            return
        pcm = np.clip(samples * 32768.0, -32768, 32767).astype("<i2").tobytes()

        def write() -> None:
            self._handle.write(pcm)

        async with self._lock:
            await asyncio.to_thread(write)
            self._samples_written += samples.size

    async def flush(self) -> None:
        """Push buffered writes to the file so `raw_path` can be read in full."""
        if self._closed:
            return
        async with self._lock:
            await asyncio.to_thread(self._handle.flush)

    async def append_silence(self, duration_ms: int) -> None:
        """Keep the timeline honest across a pause (FR-CAP-16): the gap exists in
        the file, so an utterance offset still points at the right moment."""
        count = int(duration_ms * SAMPLE_RATE / 1000)
        if count > 0:
            await self.append(np.zeros(count, dtype=np.float32))

    async def finalize(self) -> tuple[Path | None, int]:
        """Close, transcode, and return `(path, duration_ms)`."""
        if self._closed:
            return self._final_path(), self.duration_ms
        self._closed = True
        async with self._lock:
            await asyncio.to_thread(self._handle.close)
        if self._samples_written == 0:
            self._raw_path.unlink(missing_ok=True)
            return None, 0
        path = await asyncio.to_thread(self._encode)
        return path, self.duration_ms

    def _final_path(self) -> Path:
        suffix = "wav" if self.codec == "wav" else self.codec
        return self._dir / f"{self.session_id}.{suffix}"

    def _encode(self) -> Path:
        target = self._final_path()
        binary = ffmpeg_path()
        if self.codec != "wav" and binary:
            import subprocess

            cmd = [
                binary, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
                "-i", str(self._raw_path),
            ]  # fmt: skip
            if self.codec == "opus":
                cmd += ["-c:a", "libopus", "-b:a", f"{self.bitrate_kbps}k", "-vbr", "on"]
            elif self.codec == "flac":
                cmd += ["-c:a", "flac"]
            cmd.append(str(target))
            result = subprocess.run(cmd, capture_output=True, check=False)
            if result.returncode == 0 and target.exists():
                self._raw_path.unlink(missing_ok=True)
                return target
            log.warning(
                "ffmpeg failed to encode %s (%s); keeping WAV instead",
                self.session_id,
                result.stderr.decode("utf-8", "replace").strip()[:200],
            )
        elif self.codec != "wav":
            log.warning(
                "ffmpeg not found — session audio is stored as WAV, roughly 8× the size "
                "of the configured %s. Install ffmpeg to get the configured codec.",
                self.codec,
            )

        wav_target = self._dir / f"{self.session_id}.wav"
        with wave.open(str(wav_target), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            with self._raw_path.open("rb") as src:
                while block := src.read(_CHUNK):
                    out.writeframes(block)
        self._raw_path.unlink(missing_ok=True)
        return wav_target

    def abandon(self) -> None:
        with contextlib.suppress(Exception):
            self._handle.close()
        self._raw_path.unlink(missing_ok=True)
        self._closed = True


def read_pcm(path: Path) -> Samples:
    """Load a stored session as 16 kHz mono float32.

    Handles the raw `.pcm` of an interrupted session and the `.wav` of a
    finalised one directly; anything else goes through ffmpeg. This is the entry
    point for re-processing (Batch mode, plugin re-run, eval).
    """
    if path.suffix == ".pcm":
        return np.frombuffer(path.read_bytes(), dtype="<i2").astype(np.float32) / 32768.0
    if path.suffix == ".wav":
        with wave.open(str(path), "rb") as src:
            frames = src.readframes(src.getnframes())
            channels, width, rate = src.getnchannels(), src.getsampwidth(), src.getframerate()
        if width != 2:
            raise ValueError(f"{path}: expected 16-bit PCM, got {width * 8}-bit")
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            data = data.reshape(-1, channels).mean(axis=1)
        return resample_linear(data, rate, SAMPLE_RATE) if rate != SAMPLE_RATE else data

    binary = ffmpeg_path()
    if not binary:
        raise RuntimeError(
            f"reading {path.name} needs ffmpeg, which is not on PATH. "
            "Install it (`brew install ffmpeg` / `apt install ffmpeg`) or record with "
            "audio.codec = 'wav'."
        )
    import subprocess

    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "s16le",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


def resample_linear(samples: Samples, src_rate: int, dst_rate: int) -> Samples:
    """Linear resampling — adequate for offline conversion of already-captured
    audio. The live path resamples in the browser's AudioWorklet instead."""
    if src_rate == dst_rate or samples.size == 0:
        return samples
    count = round(samples.size * dst_rate / src_rate)
    positions = np.linspace(0, samples.size - 1, count, dtype=np.float64)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


MIME_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".pcm": "application/octet-stream",
}


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """`Range: bytes=start-end` → inclusive byte range, or None if absent/invalid.

    Seeking in the player depends on this: without range support a browser
    re-downloads a 40-minute file to jump to minute 30 (FR-UI-8).
    """
    if not header or not header.startswith("bytes="):
        return None
    spec = header.removeprefix("bytes=").split(",", 1)[0].strip()
    start_s, _, end_s = spec.partition("-")
    try:
        if not start_s:  # suffix range: last N bytes
            length = int(end_s)
            if length <= 0:
                return None
            return max(0, size - length), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return None
    return start, min(end, size - 1)


def iter_file(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            block = handle.read(min(_CHUNK, remaining))
            if not block:
                return
            remaining -= len(block)
            yield block


async def aiter_file(path: Path, start: int, end: int) -> AsyncIterator[bytes]:
    for block in iter_file(path, start, end):
        yield block
        await asyncio.sleep(0)


def wav_header(sample_count: int) -> bytes:
    """A 44-byte canonical WAV header, for streaming raw PCM to a player."""
    data_size = sample_count * 2
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_size)
    )
