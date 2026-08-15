/**
 * The recording state machine.
 *
 * `idle → permission → recording → stopping → done`, with `reconnecting` and
 * `error` as overlays that never replace the transcript (SRS §5.1). Keeping
 * them as separate fields rather than states is the whole reason a network blip
 * cannot interrupt a recording: nothing about the connection can move the
 * machine out of `recording`.
 */

import { create } from 'zustand'
import { api, type ModeInfo, type Speaker, type Utterance } from '../api/client'
import { ChunkBuffer, clearSession, recordPendingSession } from '../capture/buffer'
import { Recorder, type AudioProcessingOptions } from '../capture/recorder'
import { WakeLock, type WakeLockState } from '../capture/wakelock'
import { EventStream, type ServerEvent } from '../transport/events'
import { IngestClient, type LinkState } from '../transport/ingest'

export type RecordState = 'idle' | 'permission' | 'recording' | 'stopping' | 'done' | 'error'

export interface CaptureSettings extends AudioProcessingOptions {
  deviceId?: string
  languages: string[]
  targetLanguage: string
  mode: 'live' | 'balanced' | 'batch'
  vocabulary: string[]
  title?: string
  minSpeakers?: number
  maxSpeakers?: number
}

export interface Notice {
  id: string
  severity: 'info' | 'warning' | 'error'
  component: string
  message: string
  remedy?: string
}

interface RecordingState {
  state: RecordState
  sessionId: string | null
  startedAt: number | null
  elapsedMs: number
  paused: boolean
  offline: boolean

  link: LinkState
  pendingChunks: number
  droppedMs: number
  wakeLock: WakeLockState

  level: number
  utterances: Utterance[]
  partial: Utterance | null
  speakers: Record<string, Speaker>
  notices: Notice[]
  costUsd: number
  costBreakdown: Record<string, number>
  ceilingReached: boolean

  settings: CaptureSettings
  modes: ModeInfo[]

  start: (overrides?: Partial<CaptureSettings>, options?: { offline?: boolean }) => Promise<void>
  stop: () => Promise<void>
  togglePause: () => Promise<void>
  mark: () => void
  setMode: (mode: 'live' | 'balanced' | 'batch') => Promise<void>
  updateSettings: (patch: Partial<CaptureSettings>) => void
  dismissNotice: (id: string) => void
  notify: (notice: Omit<Notice, 'id'>) => void
  reset: () => void
}

// Held outside the store: they are imperative resources, not render state, and
// putting them in the store would make every audio chunk a re-render.
let recorder: Recorder | null = null
let ingest: IngestClient | null = null
let stream: EventStream | null = null
let buffer: ChunkBuffer | null = null
let wakeLock: WakeLock | null = null
let ticker: number | null = null

const SETTINGS_KEY = 'droid.capture.settings'

function loadSettings(): CaptureSettings {
  const defaults: CaptureSettings = {
    languages: ['en'],
    targetLanguage: 'en',
    mode: 'balanced',
    vocabulary: [],
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
  }
  try {
    const raw = localStorage.getItem(SETTINGS_KEY)
    return raw ? { ...defaults, ...(JSON.parse(raw) as Partial<CaptureSettings>) } : defaults
  } catch {
    return defaults
  }
}

export const useRecording = create<RecordingState>((set, get) => ({
  state: 'idle',
  sessionId: null,
  startedAt: null,
  elapsedMs: 0,
  paused: false,
  offline: false,
  link: 'closed',
  pendingChunks: 0,
  droppedMs: 0,
  wakeLock: 'released',
  level: 0,
  utterances: [],
  partial: null,
  speakers: {},
  notices: [],
  costUsd: 0,
  costBreakdown: {},
  ceilingReached: false,
  settings: loadSettings(),
  modes: [],

  notify: (notice) =>
    set((s) => ({
      notices: [...s.notices.filter((n) => n.message !== notice.message), { ...notice, id: crypto.randomUUID() }],
    })),

  dismissNotice: (id) => set((s) => ({ notices: s.notices.filter((n) => n.id !== id) })),

  updateSettings: (patch) =>
    set((s) => {
      const settings = { ...s.settings, ...patch }
      try {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings))
      } catch {
        // Private browsing. The setting still applies to this session.
      }
      return { settings }
    }),

  async start(overrides, options) {
    const settings = { ...get().settings, ...overrides }
    const offline = options?.offline ?? false
    set({
      state: 'permission',
      utterances: [],
      partial: null,
      speakers: {},
      notices: [],
      costUsd: 0,
      costBreakdown: {},
      ceilingReached: false,
    })

    try {
      let sessionId: string
      let ingestUrl = ''

      if (offline) {
        // FR-CAP-17: a local id until the server sees it. The upload path maps
        // it to a real session, so a recording made with the server down is
        // never lost, only deferred.
        sessionId = `local_${crypto.randomUUID()}`
        await recordPendingSession({
          sessionId,
          createdAt: Date.now(),
          offline: true,
          meta: { ...settings },
        })
      } else {
        const created = await api.createSession({
          title: settings.title,
          languages: settings.languages,
          target_language: settings.targetLanguage,
          mode: settings.mode,
          vocabulary: settings.vocabulary,
          min_speakers: settings.minSpeakers,
          max_speakers: settings.maxSpeakers,
          audio_constraints: {
            echoCancellation: settings.echoCancellation,
            noiseSuppression: settings.noiseSuppression,
            autoGainControl: settings.autoGainControl,
          },
        })
        sessionId = created.session_id
        ingestUrl = created.ws_url
      }

      buffer = new ChunkBuffer(sessionId)
      ingest = new IngestClient({
        url: ingestUrl,
        sessionId,
        buffer,
        offline,
        onStatus: (status) =>
          set({
            link: status.link,
            pendingChunks: status.pendingChunks,
            droppedMs: status.droppedMs,
          }),
        onError: (message, remedy) =>
          get().notify({ severity: 'error', component: 'connection', message, remedy }),
      })

      recorder = new Recorder({
        ...settings,
        chunkMs: 200,
        onChunk: (pcm, tMs) => void ingest?.send(pcm, tMs),
        onLevel: (peak) => set({ level: peak }),
        onEnded: (reason) => {
          get().notify({
            severity: 'error',
            component: 'microphone',
            message: reason,
            remedy: 'Everything recorded so far has been saved. Press Record to start a new session.',
          })
          void get().stop()
        },
      })

      await recorder.start()
      ingest.connect()

      if (!offline) {
        stream = new EventStream({
          sessionId,
          onEvent: (event) => applyEvent(set, get, event),
          onResync: async (reason) => {
            get().notify({ severity: 'info', component: 'connection', message: reason })
            const session = await api.getSession(sessionId)
            set({
              utterances: session.utterances ?? [],
              speakers: Object.fromEntries((session.speakers ?? []).map((s) => [s.id, s])),
            })
          },
          onState: () => undefined,
        })
        stream.connect()
      }

      wakeLock = new WakeLock((wakeLockState) => {
        set({ wakeLock: wakeLockState })
        if (wakeLockState === 'unsupported') {
          get().notify({
            severity: 'warning',
            component: 'screen',
            message:
              'This browser cannot keep the screen awake. Keep the screen on and this tab in front, or recording will stop.',
          })
        }
      })
      await wakeLock.acquire()

      const startedAt = Date.now()
      ticker = window.setInterval(() => set({ elapsedMs: Date.now() - startedAt }), 250)

      set({ state: 'recording', sessionId, startedAt, elapsedMs: 0, paused: false, offline })
    } catch (error) {
      await teardown()
      const message = error instanceof Error ? error.message : 'Could not start recording.'
      const remedy = (error as { remedy?: string }).remedy
      set({ state: 'error' })
      get().notify({ severity: 'error', component: 'capture', message, remedy })
    }
  },

  async stop() {
    const { sessionId, offline } = get()
    set({ state: 'stopping' })
    await recorder?.stop()
    ingest?.close()
    await teardown()

    if (sessionId && !offline) {
      try {
        await api.stopSession(sessionId)
      } catch {
        // The server may already have finalised it after the grace period.
      }
      // FR-CAP-15: nothing for this session is left in browser storage.
      await buffer?.clear()
      await clearSession(sessionId)
    }
    set({ state: 'done', paused: false, level: 0, partial: null })
  },

  async togglePause() {
    const { sessionId, paused } = get()
    if (!sessionId) return
    ingest?.control(paused ? 'resume' : 'pause')
    if (!get().offline) {
      try {
        await (paused ? api.resumeSession(sessionId) : api.pauseSession(sessionId))
      } catch {
        // The in-band control frame is the authoritative path; HTTP is a
        // fallback for a socket that is briefly down.
      }
    }
    set({ paused: !paused })
  },

  mark() {
    ingest?.control('mark', get().elapsedMs)
    get().notify({ severity: 'info', component: 'capture', message: 'Moment marked' })
  },

  async setMode(mode) {
    get().updateSettings({ mode })
    const { sessionId, state } = get()
    if (sessionId && state === 'recording') {
      await api.setMode(sessionId, mode)
    }
  },

  reset: () =>
    set({
      state: 'idle',
      sessionId: null,
      startedAt: null,
      elapsedMs: 0,
      utterances: [],
      partial: null,
      speakers: {},
      level: 0,
      notices: [],
    }),
}))

async function teardown(): Promise<void> {
  if (ticker !== null) {
    window.clearInterval(ticker)
    ticker = null
  }
  stream?.close()
  await wakeLock?.release()
  recorder = null
  ingest = null
  stream = null
  wakeLock = null
}

type Setter = (partial: Partial<RecordingState>) => void
type Getter = () => RecordingState

/** Fold a server event into the transcript. */
function applyEvent(set: Setter, get: Getter, event: ServerEvent): void {
  const data = event.data as unknown as Utterance & Record<string, unknown>
  switch (event.type) {
    case 'utterance.partial':
      // Partials rewrite in place and carry no speaker (FR-DIA-10).
      set({ partial: { ...data, is_final: false, speaker_id: null } })
      break

    case 'utterance.final': {
      const utterances = [...get().utterances]
      const index = utterances.findIndex((u) => u.utterance_id === data.utterance_id)
      if (index >= 0) utterances[index] = { ...data, is_final: true }
      else utterances.push({ ...data, is_final: true })
      utterances.sort((a, b) => a.start_ms - b.start_ms)
      set({ utterances, partial: null })
      break
    }

    case 'translation.final': {
      const utterances = get().utterances.map((u) =>
        u.utterance_id === data.utterance_id
          ? {
              ...u,
              translation: (data.translation as string | null) ?? null,
              translation_state: data.translation_state as Utterance['translation_state'],
            }
          : u,
      )
      set({ utterances })
      break
    }

    case 'speaker.changed': {
      const speaker = {
        id: data.speaker_id as unknown as string,
        label: data.label as unknown as string,
        index: data.index as unknown as number,
        display_name: (data.display_name as unknown as string | null) ?? null,
        name: (data.display_name as unknown as string | null) ?? (data.label as unknown as string),
      }
      set({ speakers: { ...get().speakers, [speaker.id]: speaker } })
      break
    }

    case 'transcript.edited': {
      const utterances = get().utterances.map((u) =>
        u.utterance_id === data.utterance_id ? { ...u, text: data.text as string, edited: true } : u,
      )
      set({ utterances })
      break
    }

    case 'session.mode_changed':
      get().notify({
        severity: 'info',
        component: 'pipeline',
        message: `Switched to ${String(data.to)} mode.`,
      })
      break

    case 'status': {
      if (typeof data.cost_usd === 'number') set({ costUsd: data.cost_usd })
      if (data.cost_breakdown && typeof data.cost_breakdown === 'object') {
        set({ costBreakdown: data.cost_breakdown as unknown as Record<string, number> })
      }
      if (data.ceiling_reached === true) {
        const switched = (data.switched_to_local as unknown as string[]) ?? []
        set({ ceilingReached: true })
        get().notify({
          severity: 'warning',
          component: 'cost',
          message: 'The cloud spend ceiling for this session was reached.',
          // FR-CFG-7 says the session continues rather than failing — but if
          // there was nothing to fall back to, saying "continuing locally"
          // would be a lie.
          remedy: switched.length
            ? `Continuing locally for ${switched.join(' and ')}. Raise the ceiling in Settings if you want cloud quality.`
            : 'No local backend is available, so cloud processing continues. Raise or remove the ceiling in Settings.',
        })
      }
      break
    }

    case 'capture.error':
    case 'plugin.error':
      get().notify({
        severity: event.type === 'plugin.error' ? 'warning' : 'error',
        component: (data.component as unknown as string) ?? (data.plugin as unknown as string) ?? 'server',
        message: (data.reason as unknown as string) ?? (data.error as unknown as string) ?? 'An error occurred.',
        remedy: data.remedy as unknown as string | undefined,
      })
      break

    default:
      break
  }
}
