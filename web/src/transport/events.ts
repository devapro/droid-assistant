/**
 * The live event stream client (SRS §5.4, FR-SES-5).
 *
 * Separate from ingest by design: a laptop can watch a session a phone is
 * recording, and the recording tab itself uses this same class to render its
 * own transcript. One code path for both means the live view and the watching
 * view cannot drift apart.
 *
 * Reconnection supplies the last sequence seen; the server replays from there,
 * or answers `resync` when the gap is too large to be worth replaying.
 */

export interface ServerEvent {
  type: string
  session_id: string
  seq: number
  ts: string
  data: Record<string, unknown>
}

export type EventStreamState = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface EventStreamOptions {
  sessionId: string
  onEvent: (event: ServerEvent) => void
  /** The stream fell too far behind; refetch the session (FR-UI-6). */
  onResync: (reason: string) => void
  onState: (state: EventStreamState) => void
  lastSeq?: number
}

export class EventStream {
  private socket: WebSocket | null = null
  private lastSeq: number
  private attempt = 0
  private closedByUs = false
  private timer: number | null = null

  constructor(private readonly options: EventStreamOptions) {
    this.lastSeq = options.lastSeq ?? 0
  }

  connect(): void {
    this.closedByUs = false
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${scheme}://${location.host}/ws/sessions/${this.options.sessionId}?last_seq=${this.lastSeq}`
    this.options.onState(this.attempt === 0 ? 'connecting' : 'reconnecting')

    const socket = new WebSocket(url)
    this.socket = socket

    socket.onopen = () => {
      this.attempt = 0
      this.options.onState('open')
    }

    socket.onmessage = (message) => {
      const payload = JSON.parse(message.data as string) as ServerEvent & { reason?: string }
      if (payload.type === 'heartbeat') return
      if (payload.type === 'resync') {
        this.lastSeq = payload.seq ?? this.lastSeq
        this.options.onResync(payload.reason ?? 'the stream fell behind')
        return
      }
      if (payload.type === 'subscribed') {
        this.lastSeq = payload.seq ?? this.lastSeq
        return
      }
      if (typeof payload.seq === 'number' && payload.seq > this.lastSeq) {
        this.lastSeq = payload.seq
      }
      this.options.onEvent(payload)
    }

    socket.onclose = () => {
      this.socket = null
      if (this.closedByUs) {
        this.options.onState('closed')
        return
      }
      this.attempt += 1
      const delay = Math.min(10_000, 500 * 2 ** Math.min(this.attempt, 4)) * (0.5 + Math.random() / 2)
      this.options.onState('reconnecting')
      this.timer = window.setTimeout(() => this.connect(), delay)
    }
  }

  close(): void {
    this.closedByUs = true
    if (this.timer !== null) window.clearTimeout(this.timer)
    this.socket?.close(1000)
    this.socket = null
  }
}
