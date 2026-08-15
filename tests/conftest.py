"""Shared fixtures.

Every test runs against mock backends and a temporary data directory, so the
suite needs no model weights, no network, and no credentials. That is what
keeps it runnable on a laptop and in CI, which is what keeps it run at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio

from droid_assistant.api.app import create_app
from droid_assistant.api.services import Services
from droid_assistant.config import Settings
from droid_assistant.domain import SAMPLE_RATE
from droid_assistant.store import Database, Repository


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def settings(data_dir: Path) -> Settings:
    return Settings(
        server={"data_dir": data_dir, "disconnect_grace_s": 1.0, "min_free_disk_mb": 0},
        capture={"languages": ["en"], "target_language": "en"},
        asr={"backend": "mock"},
        vad={"backend": "energy", "min_silence_ms": 400, "min_speech_ms": 200},
        diarization={"backend": "mock"},
        translation={"backend": "identity"},
        audio={"codec": "wav"},
        plugins={"directory": data_dir / "plugins"},
    )


@pytest_asyncio.fixture
async def db(data_dir: Path) -> AsyncIterator[Database]:
    database = Database(data_dir / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def repo(db: Database) -> Repository:
    return Repository(db)


@pytest_asyncio.fixture
async def services(settings: Settings) -> AsyncIterator[Services]:
    container = await Services.create(settings)
    await container.start()
    yield container
    await container.shutdown()


@pytest.fixture
def client(services: Services) -> Iterator[object]:
    """A TestClient sharing the `services` fixture, so a test can reach inside."""
    from fastapi.testclient import TestClient

    app = create_app(services.settings, services=services)
    with TestClient(app) as test_client:
        yield test_client


# --- audio helpers ----------------------------------------------------------


def tone(duration_ms: int, *, frequency: float = 220.0, amplitude: float = 0.3) -> np.ndarray:
    """Synthetic voiced audio: loud enough for the energy VAD to call it speech."""
    count = int(SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(count) / SAMPLE_RATE
    # Amplitude modulation makes it look less like a pure tone to anything
    # inspecting spectral flatness.
    envelope = 1 + 0.5 * np.sin(2 * np.pi * 3 * t)
    return (amplitude * np.sin(2 * np.pi * frequency * t) * envelope).astype(np.float32)


def silence(duration_ms: int) -> np.ndarray:
    return np.zeros(int(SAMPLE_RATE * duration_ms / 1000), dtype=np.float32)


def to_pcm(samples: np.ndarray) -> bytes:
    return np.clip(samples * 32768, -32768, 32767).astype("<i2").tobytes()


@pytest.fixture
def audio_helpers() -> dict[str, object]:
    return {"tone": tone, "silence": silence, "to_pcm": to_pcm}
