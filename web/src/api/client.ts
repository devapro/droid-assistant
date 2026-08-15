/** Typed HTTP client for the server API (SRS §5.2). */

export interface Utterance {
  utterance_id: string
  seq?: number
  start_ms: number
  end_ms: number
  speaker_id: string | null
  language: string | null
  text: string
  translation: string | null
  translation_state: 'none' | 'pending' | 'done' | 'failed' | 'skipped'
  confidence: number | null
  marked: boolean
  edited?: boolean
  text_original?: string | null
  words: { w: string; start_ms: number; end_ms: number }[]
  is_final?: boolean
}

export interface Speaker {
  id: string
  label: string
  index: number
  display_name: string | null
  name: string
}

export interface Artifact {
  id: string
  plugin_name: string
  kind: string
  mime: string
  content: string
  version: number
  current: boolean
  created_at: number
  metadata: Record<string, unknown>
}

export interface Session {
  id: string
  title: string | null
  state: 'recording' | 'processing' | 'ended' | 'failed'
  started_at: number
  ended_at: number | null
  duration_ms: number
  mode: 'live' | 'balanced' | 'batch'
  source_languages: string[]
  target_language: string
  vocabulary: string[]
  cloud_used: boolean
  providers_used: string[]
  cost_usd: number
  cost_breakdown: Record<string, number>
  local_only: boolean
  has_audio: boolean
  audio_duration_ms: number | null
  tags: string[]
  participants: string[]
  dropped_chunks: number
  speaker_count?: number
  artifact_kinds?: string[]
  /** How many lines were flagged as moments to come back to (FR-CAP-18). */
  marked_count?: number
  utterance_count?: number
  live?: boolean
  utterances?: Utterance[]
  speakers?: Speaker[]
  artifacts?: Artifact[]
  artifacts_stale?: boolean
  status?: Record<string, unknown> | null
}

export interface SearchHit {
  kind: 'utterance' | 'artifact'
  session_id: string
  session_title: string | null
  session_started_at: number
  snippet: string
  utterance_id: string | null
  start_ms: number | null
  speaker: string | null
  artifact_id: string | null
  artifact_kind: string | null
  plugin_name: string | null
}

export interface PluginInfo {
  name: string
  version: string
  description: string
  source: string
  enabled: boolean
  subscribes: string[]
  requires_llm: boolean
  /** Never runs on its own; produces something only when asked to. */
  on_demand: boolean
  config: Record<string, unknown>
  config_schema: JsonSchema
  last_error: string | null
  available: boolean
  unavailable_reason: string | null
}

export interface JsonSchema {
  title?: string
  type?: string
  properties?: Record<string, JsonSchemaProperty>
  required?: string[]
}

export interface JsonSchemaProperty {
  type?: string
  title?: string
  description?: string
  default?: unknown
  enum?: string[]
  minimum?: number
  maximum?: number
}

export interface Health {
  status: 'ok' | 'degraded' | 'starting'
  models: { state: 'loading' | 'ready' | 'failed'; backend: string; error: string | null; detail: string }
  version: string
  uptime_s: number
  backends: {
    asr: { backend: string; name: string; streaming: boolean; local: boolean }
    diarization: Record<string, unknown>
    translation: Record<string, unknown> & { available?: boolean }
    llm: { model: string; local: boolean; available: boolean }
  }
  gpu: { present: boolean; cuda_devices: number }
  disk: {
    available: boolean
    free_mb?: number
    total_mb?: number
    below_minimum?: boolean
    low?: boolean
  }
  local_only: boolean
  active_sessions: string[]
  plugins: { name: string; enabled: boolean; available: boolean }[]
  errors: string[]
  warnings: string[]
}

/** One page of `GET /api/sessions`. `total` counts what matches the filters. */
export interface SessionPage {
  sessions: Session[]
  total: number
  limit: number
  offset: number
  has_more: boolean
  /** Filter options across every session, not just this page. */
  facets: { languages: string[]; tags: string[] }
}

/** One entry from `GET /api/models` (FR-ASR-1, FR-ASR-7). */
export interface ModelInfo {
  id: string
  backend: string
  model: string
  /** present/absent are about weights on disk; ready/unavailable are about a cloud credential. */
  state: 'present' | 'absent' | 'downloading' | 'failed' | 'ready' | 'unavailable'
  local: boolean
  size_mb: number
  note: string
  /** null ⇒ no language restriction. A list ⇒ exactly those, and nothing else. */
  languages: string[] | null
  error: string | null
}

export interface ModelsResponse {
  models: ModelInfo[]
  default: string
  routing: Record<string, { id: string; explicit: boolean }>
  languages: string[]
  models_dir: string
  note: string
}

export interface ModeInfo {
  mode: 'live' | 'balanced' | 'batch'
  description: string
  latency_target_ms: number
  emits_partials: boolean
  uses_cloud_asr: boolean
  uses_cloud_llm: boolean
  requires_streaming_backend: boolean
}

export interface Preset {
  id: string
  name: string
  config: Record<string, unknown>
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly component: string = 'server',
    readonly remedy?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ApiError(
      0,
      'Could not reach the server.',
      'network',
      'Check that the server is running and that this device is on the same network or tailnet.',
    )
  }
  if (response.status === 204) return undefined as T
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new ApiError(
      response.status,
      (body as { error?: string; detail?: string }).error ??
        (body as { detail?: string }).detail ??
        response.statusText,
      (body as { component?: string }).component ?? 'server',
      (body as { remedy?: string }).remedy,
    )
  }
  return body as T
}

export const api = {
  health: () => request<Health>('/api/health'),

  languages: () =>
    request<{
      languages: { code: string; name: string }[]
      target_language: string
      multi_language_warning: string | null
    }>('/api/languages'),

  modes: () =>
    request<{ modes: ModeInfo[]; default: string; streaming_backend: boolean }>('/api/modes'),

  createSession: (body: Record<string, unknown>) =>
    request<{
      session_id: string
      ingest_token: string
      ws_url: string
      events_url: string
      session: Session
    }>('/api/sessions', { method: 'POST', body: JSON.stringify(body) }),

  listSessions: (params: Record<string, string | number | undefined> = {}) => {
    const query = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value))
    }
    return request<SessionPage>(`/api/sessions?${query}`)
  },

  getSession: (id: string) => request<Session>(`/api/sessions/${id}`),

  updateSession: (id: string, body: Record<string, unknown>) =>
    request<Session>(`/api/sessions/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),

  stopSession: (id: string) => request<Session>(`/api/sessions/${id}/stop`, { method: 'POST' }),

  pauseSession: (id: string) => request<{ paused: boolean }>(`/api/sessions/${id}/pause`, { method: 'POST' }),

  resumeSession: (id: string) =>
    request<{ paused: boolean }>(`/api/sessions/${id}/resume`, { method: 'POST' }),

  setMode: (id: string, mode: string) =>
    request<{ requested_mode: string; applies: string }>(`/api/sessions/${id}/mode`, {
      method: 'PATCH',
      body: JSON.stringify({ mode }),
    }),

  deleteSession: (id: string) => request<void>(`/api/sessions/${id}`, { method: 'DELETE' }),

  editUtterance: (id: string, body: { text?: string; marked?: boolean }) =>
    request<Utterance & { artifacts_stale: boolean }>(`/api/utterances/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  renameSpeaker: (id: string, displayName: string | null) =>
    request<Speaker>(`/api/speakers/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ display_name: displayName }),
    }),

  artifacts: (sessionId: string) =>
    request<{ artifacts: Artifact[]; stale: boolean }>(`/api/sessions/${sessionId}/artifacts`),

  /**
   * Run one plugin over a session, or over `utteranceIds` of it. Scoping is
   * what "make an action item from this line" posts; omitting it means the
   * whole conversation, which is what a re-run after an edit wants.
   */
  runPlugin: (sessionId: string, name: string, utteranceIds?: string[]) =>
    request<{ ran: boolean; artifact: Artifact | null; note?: string }>(
      `/api/sessions/${sessionId}/plugins/${name}/run`,
      {
        method: 'POST',
        body: JSON.stringify(utteranceIds?.length ? { utterance_ids: utteranceIds } : {}),
      },
    ),

  plugins: () =>
    request<{ plugins: PluginInfo[]; discovery_errors: string[]; trust_notice: string }>('/api/plugins'),

  patchPlugin: (name: string, body: { enabled?: boolean; config?: Record<string, unknown> }) =>
    request<PluginInfo>(`/api/plugins/${name}`, { method: 'PATCH', body: JSON.stringify(body) }),

  search: (q: string, sessionId?: string) => {
    const query = new URLSearchParams({ q })
    if (sessionId) query.set('session_id', sessionId)
    return request<{ hits: SearchHit[]; count: number }>(`/api/search?${query}`)
  },

  config: () =>
    request<{ config: Record<string, never>; log_levels: Record<string, string>; note: string }>(
      '/api/config',
    ),

  patchConfig: (body: Record<string, unknown>) =>
    request<{ config: Record<string, never>; applies_to: string }>('/api/config', {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  models: () => request<ModelsResponse>('/api/models'),

  downloadModel: (id: string) =>
    request<{ id: string; state: string }>('/api/models/download', {
      method: 'POST',
      body: JSON.stringify({ id }),
    }),

  presets: () => request<{ presets: Preset[] }>('/api/presets'),

  savePreset: (name: string, config: Record<string, unknown>) =>
    request<Preset>('/api/presets', { method: 'PUT', body: JSON.stringify({ name, config }) }),

  deletePreset: (id: string) => request<void>(`/api/presets/${id}`, { method: 'DELETE' }),

  exportUrl: (sessionId: string, format: string) => `/api/sessions/${sessionId}/export?format=${format}`,

  audioUrl: (sessionId: string) => `/api/sessions/${sessionId}/audio`,
}
