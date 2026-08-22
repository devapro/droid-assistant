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

/** Close codes the server uses for a refusal that a retry cannot change. */
const FINAL_CLOSE_CODES = [4401, 4404]

export interface EventStreamOptions {
  sessionId: string
  onEvent: (event: ServerEvent) => void
  /** The stream fell too far behind; refetch the session (FR-UI-6). */
  onResync: (reason: string) => void
  onState: (state: EventStreamState) => void
  /**
   * The server's opening frame: the session's live pipeline status, or `null`
   * for a session that is no longer running.
   *
   * Worth surfacing rather than dropping, because some of what a viewer needs is
   * *state* rather than an event — which recogniser is transcribing, say. Arriving
   * only as an event would mean showing nothing until the next one happened, and
   * on reconnection would mean showing nothing again.
   */
  onSubscribed?: (info: { live: boolean; status: Record<string, unknown> | null }) => void
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
      const payload = JSON.parse(message.data as string) as ServerEvent & {
        reason?: string
        live?: boolean
        status?: Record<string, unknown> | null
      }
      if (payload.type === 'heartbeat') return
      if (payload.type === 'resync') {
        this.lastSeq = payload.seq ?? this.lastSeq
        this.options.onResync(payload.reason ?? 'the stream fell behind')
        return
      }
      if (payload.type === 'subscribed') {
        this.lastSeq = payload.seq ?? this.lastSeq
        this.options.onSubscribed?.({
          live: Boolean(payload.live),
          status: payload.status ?? null,
        })
        return
      }
      if (typeof payload.seq === 'number' && payload.seq > this.lastSeq) {
        this.lastSeq = payload.seq
      }
      this.options.onEvent(payload)
    }

    socket.onclose = (event) => {
      this.socket = null
      if (this.closedByUs || FINAL_CLOSE_CODES.includes(event.code)) {
        // Retrying will not help: the session does not exist, or this origin is
        // not allowed to watch it. Left to reconnect, the client hammers a
        // permanent refusal for as long as the tab is open.
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
