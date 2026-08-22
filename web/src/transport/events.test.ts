/**
 * The reconnect policy.
 *
 * Worth pinning because getting it wrong is not a cosmetic failure. A client
 * that retries a refusal no retry can change, or that reopens the stream on
 * every event it receives, replays the whole backlog per attempt and takes the
 * tab down with it — `net::ERR_INSUFFICIENT_RESOURCES`, and a server flooded
 * with reads while it is trying to record.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { EventStream, type EventStreamState } from './events'

class FakeSocket {
  static opened: FakeSocket[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  closed = false

  constructor(readonly url: string) {
    FakeSocket.opened.push(this)
  }

  close(): void {
    this.closed = true
  }
}

function stream(overrides: { onResync?: (reason: string) => void } = {}) {
  const states: EventStreamState[] = []
  const events: unknown[] = []
  const instance = new EventStream({
    sessionId: 'sess_a',
    onEvent: (event) => events.push(event),
    onResync: overrides.onResync ?? (() => undefined),
    onState: (state) => states.push(state),
  })
  instance.connect()
  return { instance, states, events }
}

const last = (): FakeSocket => {
  const socket = FakeSocket.opened.at(-1)
  if (!socket) throw new Error('no socket was opened')
  return socket
}

describe('EventStream', () => {
  beforeEach(() => {
    FakeSocket.opened = []
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
    vi.stubGlobal('location', { protocol: 'http:', host: 'localhost:8000' })
    vi.stubGlobal('window', globalThis)
  })

  it('gives up when the server says there is no such session', () => {
    const { states } = stream()
    last().onclose?.({ code: 4404 })
    vi.advanceTimersByTime(120_000)

    expect(FakeSocket.opened).toHaveLength(1)
    expect(states.at(-1)).toBe('closed')
  })

  it('gives up when this origin is not allowed to watch', () => {
    stream()
    last().onclose?.({ code: 4401 })
    vi.advanceTimersByTime(120_000)

    expect(FakeSocket.opened).toHaveLength(1)
  })

  it('reconnects after a dropped connection', () => {
    const { states } = stream()
    last().onclose?.({ code: 1006 })
    vi.advanceTimersByTime(30_000)

    expect(FakeSocket.opened).toHaveLength(2)
    expect(states).toContain('reconnecting')
  })

  it('reconnects from the last sequence it saw, not from the start', () => {
    stream()
    last().onmessage?.({
      data: JSON.stringify({ type: 'utterance.final', session_id: 'sess_a', seq: 7, data: {} }),
    })
    last().onclose?.({ code: 1006 })
    vi.advanceTimersByTime(30_000)

    // Asking from 0 again would replay every event of the session on every
    // attempt, which is how one dropped connection becomes a flood.
    expect(last().url).toContain('last_seq=7')
  })
})
