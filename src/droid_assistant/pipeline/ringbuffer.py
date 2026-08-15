"""A bounded audio ring buffer that drops oldest rather than growing (FR-SIG-1).

The choice of *which* data to drop is the whole design. Under a stalled consumer
— a model reloading, a GPU busy, a disk stalling — something must give, and the
options are: grow until the process is killed, drop the newest audio, or drop
the oldest.

Dropping oldest is right here because the pipeline is chasing realtime. Newly
arrived speech is what the user is waiting to see; audio from thirty seconds ago
that nothing has consumed is already past its latency budget. Drops are counted
and surfaced (NFR-PERF-6), because silent loss in a transcript is the failure
users cannot detect.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import numpy as np

from ..domain import SAMPLE_RATE, AudioBuffer, Samples


@dataclass(slots=True, frozen=True)
class RingStats:
    written_samples: int
    dropped_samples: int
    drop_events: int
    available_samples: int
    capacity_samples: int

    @property
    def dropped_ms(self) -> int:
        return int(self.dropped_samples * 1000 / SAMPLE_RATE)

    @property
    def available_ms(self) -> int:
        return int(self.available_samples * 1000 / SAMPLE_RATE)

    @property
    def fill_ratio(self) -> float:
        return self.available_samples / self.capacity_samples if self.capacity_samples else 0.0


class RingBuffer:
    """Single-producer, single-consumer, fixed capacity.

    Writes never block and never allocate beyond the fixed backing array; reads
    await new data. Sample positions are absolute for the lifetime of the buffer,
    so a consumer can always tell where in the session it is even after a drop.
    """

    def __init__(self, capacity_ms: int = 60_000, sample_rate: int = SAMPLE_RATE) -> None:
        self._capacity = max(1, int(capacity_ms * sample_rate / 1000))
        self._sample_rate = sample_rate
        self._data = np.zeros(self._capacity, dtype=np.float32)
        self._write_pos = 0  # absolute count of samples ever written
        self._read_pos = 0  # absolute position of the next unread sample
        self._dropped = 0
        self._drop_events = 0
        self._closed = False
        self._data_available = asyncio.Event()

    # --- properties ---------------------------------------------------------

    @property
    def capacity_ms(self) -> int:
        return int(self._capacity * 1000 / self._sample_rate)

    @property
    def available(self) -> int:
        return self._write_pos - self._read_pos

    @property
    def dropped_samples(self) -> int:
        return self._dropped

    def stats(self) -> RingStats:
        return RingStats(
            written_samples=self._write_pos,
            dropped_samples=self._dropped,
            drop_events=self._drop_events,
            available_samples=self.available,
            capacity_samples=self._capacity,
        )

    # --- producer -----------------------------------------------------------

    def write(self, samples: Samples) -> int:
        """Append. Returns the number of samples dropped to make room."""
        count = samples.size
        if count == 0 or self._closed:
            return 0

        if count >= self._capacity:
            # A single write larger than the buffer: keep its tail, which is the
            # most recent audio, and account for everything discarded.
            #
            # The tail must land at the offset the read cursor will point at,
            # not at index 0 — positions are absolute, so the placement has to
            # stay congruent with them modulo the capacity.
            discarded = count - self._capacity
            tail = samples[-self._capacity :]
            dropped_existing = self.available
            start = (self._write_pos + discarded) % self._capacity
            end = start + self._capacity
            if end <= self._capacity:
                self._data[start:end] = tail
            else:
                split = self._capacity - start
                self._data[start:] = tail[:split]
                self._data[: end - self._capacity] = tail[split:]
            self._write_pos += count
            self._read_pos = self._write_pos - self._capacity
            self._note_drop(discarded + dropped_existing)
            self._data_available.set()
            return discarded + dropped_existing

        start = self._write_pos % self._capacity
        end = start + count
        if end <= self._capacity:
            self._data[start:end] = samples
        else:
            split = self._capacity - start
            self._data[start:] = samples[:split]
            self._data[: end - self._capacity] = samples[split:]
        self._write_pos += count

        overrun = self.available - self._capacity
        dropped = 0
        if overrun > 0:
            self._read_pos += overrun
            dropped = overrun
            self._note_drop(overrun)

        self._data_available.set()
        return dropped

    def _note_drop(self, count: int) -> None:
        if count > 0:
            self._dropped += count
            self._drop_events += 1

    # --- consumer -----------------------------------------------------------

    def read(self, count: int | None = None) -> AudioBuffer:
        """Take up to `count` samples (all available by default)."""
        take = self.available if count is None else min(count, self.available)
        if take <= 0:
            return AudioBuffer.empty(start_ms=self.read_position_ms)
        start_ms = self.read_position_ms
        start = self._read_pos % self._capacity
        end = start + take
        if end <= self._capacity:
            chunk = self._data[start:end].copy()
        else:
            chunk = np.concatenate((self._data[start:], self._data[: end - self._capacity]))
        self._read_pos += take
        if self.available == 0:
            self._data_available.clear()
        return AudioBuffer(chunk, start_ms=start_ms, sample_rate=self._sample_rate)

    def peek(self, count: int) -> AudioBuffer:
        """Look at the next `count` samples without consuming — the sliding
        window in Live mode re-reads overlapping audio (SRS §6.2)."""
        take = min(count, self.available)
        if take <= 0:
            return AudioBuffer.empty(start_ms=self.read_position_ms)
        start = self._read_pos % self._capacity
        end = start + take
        chunk = (
            self._data[start:end].copy()
            if end <= self._capacity
            else np.concatenate((self._data[start:], self._data[: end - self._capacity]))
        )
        return AudioBuffer(chunk, start_ms=self.read_position_ms, sample_rate=self._sample_rate)

    def discard(self, count: int) -> None:
        self._read_pos = min(self._write_pos, self._read_pos + max(0, count))
        if self.available == 0:
            self._data_available.clear()

    @property
    def read_position_ms(self) -> int:
        return int(self._read_pos * 1000 / self._sample_rate)

    @property
    def write_position_ms(self) -> int:
        return int(self._write_pos * 1000 / self._sample_rate)

    async def wait(self, min_samples: int = 1, timeout: float | None = None) -> bool:
        """Await at least `min_samples`, or the buffer closing. False on timeout."""
        while not self._closed and self.available < min_samples:
            self._data_available.clear()
            if self.available >= min_samples:
                return True
            try:
                await asyncio.wait_for(self._data_available.wait(), timeout=timeout)
            except TimeoutError:
                return False
        return self.available >= min_samples

    def close(self) -> None:
        self._closed = True
        self._data_available.set()

    @property
    def closed(self) -> bool:
        return self._closed

    def seek_to(self, position_ms: int) -> None:
        """Move the read cursor to an absolute session position, clamped to what
        is still resident. Used when a mode switch changes the window policy."""
        target = int(position_ms * self._sample_rate / 1000)
        oldest = max(0, self._write_pos - self._capacity)
        self._read_pos = min(self._write_pos, max(oldest, target))
