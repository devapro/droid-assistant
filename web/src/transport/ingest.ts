/**
 * The ingest WebSocket client (FR-CAP-5, FR-CAP-6, SRS §5.3).
 *
 * Sends a JSON control frame then its binary payload, tracks acknowledgements,
 * and on reconnect retransmits **from the first gap** — the server acknowledges
 * the highest contiguous sequence, so everything above it is resent whether or
 * not it arrived. Re-sending a chunk the server already has is free; failing to
 * re-send one it lacks is a hole in the meeting.
 *
 * Reconnection backs off exponentially with jitter, and capture never stops
 * while it happens: chunks accumulate in the buffer and drain on reconnect
 * (SRS §5.1 — "recording must never be interrupted to show a network message").
 */

import { ChunkBuffer, storeOffline, type Chunk } from '../capture/buffer'

export type LinkState = 'connecting' | 'connected' | 'reconnecting' | 'offline' | 'closed'

export interface IngestStatus {
  link: LinkState
  ackedThrough: number
  pendingChunks: number
  bufferedMs: number
  droppedMs: number
  attempt: number
}

export interface IngestOptions {
  url: string
  sessionId: string
  buffer: ChunkBuffer
  onStatus: (status: IngestStatus) => void
  onError: (message: string, remedy?: string) => void
  /** True when the session is being recorded with no server at all (FR-CAP-17). */
  offline?: boolean
}

const MAX_BACKOFF_MS = 15_000

export class IngestClient {
  private socket: WebSocket | null = null
  private seq = 0
  private attempt = 0
  private closedByUs = false
  private reconnectTimer: number | null = null
  private status: IngestStatus = {
    link: 'connecting',
    ackedThrough: -1,
    pendingChunks: 0,
    bufferedMs: 0,
    droppedMs: 0,
    attempt: 0,
  }

  constructor(private readonly options: IngestOptions) {}

  get currentSeq(): number {
    return this.seq
  }

  get link(): LinkState {
    return this.status.link
  }

  connect(): void {
    if (this.options.offline) {
      this.update({ link: 'offline' })
      return
    }
    this.closedByUs = false
    this.update({ link: this.attempt === 0 ? 'connecting' : 'reconnecting', attempt: this.attempt })

    let socket: WebSocket
    try {
      socket = new WebSocket(this.options.url)
    } catch {
      this.scheduleReconnect()
      return
    }
    socket.binaryType = 'arraybuffer'
    this.socket = socket

    socket.onopen = () => {
      this.attempt = 0
      this.update({ link: 'connected', attempt: 0 })
      void this.retransmit()
    }

    socket.onmessage = (event) => {
      if (typeof event.data !== 'string') return
      const message = JSON.parse(event.data) as Record<string, unknown>
      if (message.type === 'ack') {
        const through = message.through_seq as number
        void this.options.buffer.ack(through)
        this.update({
          ackedThrough: through,
          pendingChunks: this.options.buffer.pendingCount,
          bufferedMs: (message.buffered_ms as number) ?? 0,
          droppedMs: (message.dropped_ms as number) ?? 0,
        })
      }
    }

    socket.onclose = (event) => {
      this.socket = null
      if (this.closedByUs) {
        this.update({ link: 'closed' })
        return
      }
      // 4401/4404 are ours: the token is invalid or the session is gone, and
      // retrying will never help.
      if (event.code === 4401 || event.code === 4404) {
        this.update({ link: 'closed' })
        this.options.onError(
          event.reason || 'the server rejected this recording session',
          'Stop and start a new recording.',
        )
        return
      }
      this.scheduleReconnect()
    }

    socket.onerror = () => {
      // `onclose` always follows; reconnection is handled there.
    }
  }

  private scheduleReconnect(): void {
    if (this.closedByUs || this.reconnectTimer !== null) return
    this.attempt += 1
    const base = Math.min(MAX_BACKOFF_MS, 500 * 2 ** Math.min(this.attempt, 5))
    const delay = base * (0.5 + Math.random() / 2)
    this.update({ link: 'reconnecting', attempt: this.attempt })
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null
      this.connect()
    }, delay)
  }

  /** Resend everything above the last acknowledged contiguous sequence. */
  private async retransmit(): Promise<void> {
    const from = this.options.buffer.ackedThrough + 1
    const pending = await this.options.buffer.since(from)
    for (const chunk of pending) {
      this.transmit(chunk)
    }
    this.update({ pendingChunks: this.options.buffer.pendingCount })
  }

  async send(pcm: ArrayBuffer, tMs: number): Promise<void> {
    const chunk: Chunk = { seq: this.seq++, tMs, pcm }

    if (this.options.offline) {
      const stored = await storeOffline(this.options.sessionId, chunk)
      if (!stored) {
        this.options.onError(
          'Local storage is full, so this recording cannot continue offline.',
          'Free some space, or reconnect to the server.',
        )
      }
      this.update({ pendingChunks: this.seq })
      return
    }

    await this.options.buffer.add(chunk)
    this.transmit(chunk)
    this.update({
      pendingChunks: this.options.buffer.pendingCount,
      droppedMs: this.options.buffer.dropped * 200,
    })
  }

  private transmit(chunk: Chunk): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return
    this.socket.send(
      JSON.stringify({
        seq: chunk.seq,
        t_ms: chunk.tMs,
        codec: 'pcm_s16le_16k',
        samples: chunk.pcm.byteLength / 2,
      }),
    )
    // The buffer owns this ArrayBuffer until it is acknowledged, so send a copy
    // rather than letting the socket detach it.
    this.socket.send(chunk.pcm.slice(0))
  }

  /** In-band control, so pause and mark need no HTTP round trip. */
  control(action: 'pause' | 'resume' | 'mark', tMs?: number): void {
    if (this.socket?.readyState !== WebSocket.OPEN) return
    this.socket.send(JSON.stringify({ type: 'control', action, t_ms: tMs }))
  }

  close(): void {
    this.closedByUs = true
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.socket?.close(1000, 'recording stopped')
    this.socket = null
    this.update({ link: 'closed' })
  }

  private update(patch: Partial<IngestStatus>): void {
    this.status = { ...this.status, ...patch }
    this.options.onStatus(this.status)
  }
}
