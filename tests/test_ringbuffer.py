"""Ring buffer (FR-SIG-1).

The property under test is the one the requirement actually cares about: under
a stalled consumer, memory stays bounded, the *oldest* data is what goes, and
every dropped sample is counted. Silent loss is the failure mode users cannot
detect, so the counter is as important as the bound.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from droid_assistant.domain import SAMPLE_RATE
from droid_assistant.pipeline.ringbuffer import RingBuffer


def ramp(count: int, start: int = 0) -> np.ndarray:
    """Distinguishable samples, so a test can tell *which* audio survived."""
    return np.arange(start, start + count, dtype=np.float32)


def test_write_and_read_round_trips() -> None:
    ring = RingBuffer(capacity_ms=1000)
    ring.write(ramp(1600))
    buffer = ring.read()
    assert buffer.samples.size == 1600
    assert np.array_equal(buffer.samples, ramp(1600))
    assert ring.available == 0


def test_wraps_around_capacity() -> None:
    ring = RingBuffer(capacity_ms=100)  # 1600 samples
    ring.write(ramp(1000))
    assert ring.read().samples.size == 1000
    ring.write(ramp(1000, start=1000))
    assert np.array_equal(ring.read().samples, ramp(1000, start=1000))


def test_drops_oldest_and_counts_it() -> None:
    ring = RingBuffer(capacity_ms=100)  # 1600 samples
    ring.write(ramp(1600))
    dropped = ring.write(ramp(400, start=1600))
    assert dropped == 400
    assert ring.dropped_samples == 400

    # What survives is the *newest* 1600 samples: the pipeline is chasing
    # realtime, so old unconsumed audio is already past its latency budget.
    remaining = ring.read()
    assert remaining.samples.size == 1600
    assert remaining.samples[0] == 400
    assert remaining.samples[-1] == 1999


def test_write_larger_than_capacity_keeps_the_tail() -> None:
    ring = RingBuffer(capacity_ms=100)
    dropped = ring.write(ramp(5000))
    assert dropped == 5000 - 1600
    survived = ring.read()
    assert survived.samples.size == 1600
    assert survived.samples[-1] == 4999


def test_memory_is_bounded_under_a_stalled_consumer() -> None:
    ring = RingBuffer(capacity_ms=1000)
    for i in range(200):
        ring.write(ramp(1600, start=i * 1600))
    stats = ring.stats()
    assert stats.available_samples <= stats.capacity_samples
    assert stats.dropped_samples > 0
    assert stats.drop_events > 0
    assert ring._data.nbytes == SAMPLE_RATE * 4  # the backing array never grew


def test_positions_are_absolute_across_drops() -> None:
    ring = RingBuffer(capacity_ms=100)
    ring.write(ramp(1600))
    ring.write(ramp(1600, start=1600))
    # Even after a drop, the read cursor reports where in the session it is, so
    # a consumer can align what it receives with the timeline.
    assert ring.read_position_ms == 100
    ring.read()
    assert ring.read_position_ms == 200


def test_peek_does_not_consume() -> None:
    ring = RingBuffer(capacity_ms=1000)
    ring.write(ramp(800))
    assert ring.peek(400).samples.size == 400
    assert ring.available == 800
    ring.discard(400)
    assert ring.available == 400


@pytest.mark.asyncio
async def test_wait_returns_when_data_arrives() -> None:
    ring = RingBuffer(capacity_ms=1000)

    async def produce() -> None:
        await asyncio.sleep(0.02)
        ring.write(ramp(1600))

    task = asyncio.create_task(produce())
    assert await ring.wait(min_samples=1600, timeout=1.0)
    await task


@pytest.mark.asyncio
async def test_wait_returns_false_on_timeout() -> None:
    ring = RingBuffer(capacity_ms=1000)
    assert not await ring.wait(min_samples=1600, timeout=0.05)


@pytest.mark.asyncio
async def test_wait_unblocks_on_close() -> None:
    """A consumer awaiting audio must not hang when the session ends."""
    ring = RingBuffer(capacity_ms=1000)

    async def close_soon() -> None:
        await asyncio.sleep(0.02)
        ring.close()

    task = asyncio.create_task(close_soon())
    assert not await ring.wait(min_samples=1600, timeout=1.0)
    assert ring.closed
    await task


def test_seek_clamps_to_resident_audio() -> None:
    ring = RingBuffer(capacity_ms=100)
    ring.write(ramp(4800))  # three capacities' worth
    ring.seek_to(0)  # ask for audio long since overwritten
    # Clamped to the oldest sample still present, not silently reading garbage.
    assert ring.read_position_ms == 200
