/**
 * Screen 3 — Session detail (SRS §5.1).
 *
 * Tabs rather than scrolling separate the transcript from the artifacts: they
 * answer different questions and one should never bury the other.
 *
 * The player follows the transcript and the transcript follows the player
 * (FR-UI-8). Editing is inline on any line, marks the line as edited, keeps the
 * original retrievable, and offers to re-run the artifacts that the edit has
 * just made stale (FR-SES-8, FR-SES-9).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  api,
  type Artifact,
  type PluginInfo,
  type Session,
  type Speaker,
  type Utterance,
} from '../api/client'
import { EventStream } from '../transport/events'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CostBreakdown } from '../components/CostBreakdown'
import { Markdown } from '../components/Markdown'
import { Transcript, type TranscriptView } from '../components/Transcript'
import { Button, EmptyState, Pill, clock, duration, relativeDate } from '../components/primitives'
import { t } from '../i18n'
import { useActionItems } from '../state/actionItems'

type Tab = 'transcript' | string

/**
 * Whether an artifact is meant for a person to read.
 *
 * `action_items` emits its list twice — Markdown to read, JSON for anything
 * downstream — and both used to claim a tab, so the reader was offered
 * "Action Items Json" beside "Action Items". The JSON is still exported and
 * still on the API; it just is not a page in a reading view.
 */
const readable = (artifact: Artifact) => !artifact.mime.includes('json')


export function SessionDetail({
  sessionId,
  focusUtteranceId,
  onBack,
}: {
  sessionId: string
  focusUtteranceId?: string
  onBack: () => void
}) {
  const strings = t()
  const [session, setSession] = useState<Session | null>(null)
  const [tab, setTab] = useState<Tab>('transcript')
  const [view, setView] = useState<TranscriptView>('both')
  const [playing, setPlaying] = useState(false)
  const [positionMs, setPositionMs] = useState(0)
  const [rerunning, setRerunning] = useState<string | null>(null)
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const audio = useRef<HTMLAudioElement>(null)
  const actionItems = useActionItems(sessionId)

  const load = useCallback(async () => {
    try {
      setSession(await api.getSession(sessionId))
    } catch (err) {
      setError(err instanceof Error ? err.message : strings.errors.generic)
    }
  }, [sessionId, strings.errors.generic])

  useEffect(() => {
    void load()
  }, [load])

  // Which plugins could be run from here. A control that posts to a disabled
  // plugin is worse than no control: it fails at the moment someone expected
  // something to happen, with a reason that belongs in Settings.
  useEffect(() => {
    api
      .plugins()
      .then((body) => setPlugins(body.plugins))
      .catch(() => setPlugins([]))
  }, [])

  // A session still recording, or still running plugins, keeps updating. This
  // is the same stream the Record view uses (FR-SES-5).
  useEffect(() => {
    if (!session || (session.state === 'ended' && !session.artifacts_stale)) return
    if (session.state === 'ended' && session.artifacts?.length) return
    const stream = new EventStream({
      sessionId,
      onEvent: (event) => {
        if (['utterance.final', 'translation.final', 'artifact.created', 'session.end', 'speaker.changed'].includes(event.type)) {
          void load()
        }
      },
      onResync: () => void load(),
      onState: () => undefined,
    })
    stream.connect()
    return () => stream.close()
  }, [session?.state, session?.artifacts?.length, sessionId, load, session])

  const speakers: Record<string, Speaker> = Object.fromEntries(
    (session?.speakers ?? []).map((speaker) => [speaker.id, speaker]),
  )

  // Playback highlights the line currently being spoken (FR-UI-8).
  const activeUtterance = session?.utterances?.find(
    (utterance) => positionMs >= utterance.start_ms && positionMs <= utterance.end_ms,
  )

  const seekTo = (utterance: Utterance) => {
    if (!audio.current || !session?.has_audio) return
    audio.current.currentTime = utterance.start_ms / 1000
    void audio.current.play()
  }

  const edit = async (utterance: Utterance, text: string) => {
    await api.editUtterance(utterance.utterance_id, text ? { text } : {})
    await load()
  }

  const rename = async (speaker: Speaker) => {
    const name = window.prompt(strings.session.renameHint, speaker.name)
    if (name === null) return
    await api.renameSpeaker(speaker.id, name.trim() || null)
    await load()
  }

  const rerun = async (pluginName: string) => {
    setRerunning(pluginName)
    setError(null)
    try {
      const result = await api.runPlugin(sessionId, pluginName)
      await load()
      if (result.artifact) {
        // Land on what was just produced. Without this the view stays on the
        // plugin's "generate" tab — which no longer exists in the tab strip,
        // because the plugin is no longer idle — so the button sits there
        // apparently having done nothing.
        setTab(result.artifact.kind)
      } else {
        setError(result.note ?? strings.session.producedNothing(pluginName))
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : strings.errors.generic)
    } finally {
      setRerunning(null)
    }
  }

  const extractActionItem = async (utterance: Utterance) => {
    await actionItems.extract(utterance)
    // The list is what the artifact tabs are built from, so it has to come back.
    await load()
  }

  if (error && !session) {
    return (
      <div className="p-6">
        <Button variant="ghost" onClick={onBack}>
          ‹ {strings.history.title}
        </Button>
        <EmptyState title={error} action={strings.common.retry} />
      </div>
    )
  }
  if (!session) return <p className="text-fg-dim p-6 text-sm">{strings.common.loading}</p>

  const current = (session.artifacts ?? []).filter((a) => a.current)
  const runnable = plugins.filter((plugin) => plugin.enabled && plugin.available)
  // A plugin that has produced nothing gets a tab anyway, so there is somewhere
  // to press "generate". Keyed by plugin rather than by artifact kind, because
  // a plugin does not declare in advance what kinds it emits — and judged on
  // *all* its artifacts, so one that only emits JSON is not called idle.
  const idle = runnable.filter(
    (plugin) => !current.some((artifact) => artifact.plugin_name === plugin.name),
  )
  const tabs: { id: Tab; label: string }[] = [
    { id: 'transcript', label: strings.session.transcript },
    ...current.filter(readable).map((a) => ({ id: a.kind, label: a.kind.replace(/_/g, ' ') })),
    ...idle.map((plugin) => ({ id: `plugin:${plugin.name}`, label: plugin.name.replace(/_/g, ' ') })),
  ]
  // Only while that plugin really is idle. Once it has produced something the
  // tab is gone from the strip, and leaving the panel behind would keep showing
  // a "generate" button for work that is already done.
  const named = tab.startsWith('plugin:') ? tab.slice('plugin:'.length) : null
  const idlePlugin = idle.some((plugin) => plugin.name === named) ? named : null
  const shown = idlePlugin ?? (tabs.some((entry) => entry.id === tab) ? tab : 'transcript')
  const canExtract = actionItems.available

  return (
    <div className="flex h-full flex-col">
      <header className="border-line border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <button type="button" onClick={onBack} className="text-fg-dim hover:text-fg text-sm">
            ‹ {strings.history.title}
          </button>
          <h1 className="min-w-0 flex-1 truncate text-center font-medium">{session.title ?? 'Untitled'}</h1>
          <details className="relative">
            <summary className="text-fg-dim hover:text-fg cursor-pointer list-none px-2">⋯</summary>
            <div className="bg-surface-1 border-line absolute right-0 z-20 mt-1 w-56 rounded-xl border p-1 shadow-xl">
              {(['md', 'json', 'srt', 'vtt'] as const).map((format) => (
                <a
                  key={format}
                  href={api.exportUrl(sessionId, format)}
                  download
                  className="hover:bg-surface-2 block rounded-lg px-3 py-2 text-sm"
                >
                  {strings.session.export} · {format.toUpperCase()}
                </a>
              ))}
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                className="hover:bg-danger/15 text-danger block w-full rounded-lg px-3 py-2 text-left text-sm"
              >
                {strings.session.delete}
              </button>
              {/* The record of whether anything left the server lives here,
                  together with what it cost. */}
              <div className="border-line mt-1 border-t px-3 py-2">
                <p className="text-fg-dim mb-1.5 text-xs">
                  {session.cloud_used
                    ? strings.session.cloudNotice(
                        session.providers_used.join(', ') || 'a cloud service',
                      )
                    : 'Nothing left this server.'}
                </p>
                <CostBreakdown
                  total={session.cost_usd}
                  breakdown={session.cost_breakdown ?? {}}
                />
              </div>
            </div>
          </details>
        </div>

        <p className="text-fg-dim mt-1 text-sm">
          {relativeDate(session.started_at)} · {duration(session.duration_ms)} ·{' '}
          {(session.source_languages[0] ?? 'auto').toUpperCase()}→{session.target_language.toUpperCase()}
          {session.dropped_chunks > 0 && ' · some audio was dropped'}
        </p>
        {session.tags.length > 0 && (
          <p className="text-fg-dim mt-1 text-xs">{session.tags.map((tag) => `#${tag}`).join(' ')}</p>
        )}
      </header>

      <nav className="border-line flex gap-1 border-b px-2" role="tablist">
        {tabs.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            aria-selected={tab === entry.id}
            onClick={() => setTab(entry.id)}
            className={`border-b-2 px-3 py-2 text-sm capitalize transition ${
              tab === entry.id ? 'border-accent text-fg' : 'text-fg-dim border-transparent'
            }`}
          >
            {entry.label}
          </button>
        ))}
        {shown === 'transcript' && session.target_language && (
          <select
            value={view}
            onChange={(event) => setView(event.target.value as TranscriptView)}
            className="text-fg-dim ml-auto self-center bg-transparent py-1 text-xs"
            aria-label="Transcript view"
          >
            <option value="both">{strings.session.sideBySide}</option>
            <option value="original">{strings.session.original}</option>
            <option value="translation">{strings.session.translation}</option>
          </select>
        )}
      </nav>

      {session.artifacts_stale && shown !== 'transcript' && (
        <p className="bg-warn/15 text-warn px-4 py-2 text-sm">{strings.session.stale}</p>
      )}

      {/* Errors were only rendered when the session itself failed to load, so a
          plugin run that failed after that reported nothing at all — the button
          simply stopped spinning. Dismissable, because it is not fatal. */}
      {(error || actionItems.error) && (
        <button
          type="button"
          onClick={() => setError(null)}
          className="bg-warn/15 text-warn w-full px-4 py-2 text-left text-sm"
        >
          ⚠ {error ?? actionItems.error} <span className="opacity-60">— dismiss</span>
        </button>
      )}

      {actionItems.notice && shown === 'transcript' && (
        <p className="bg-accent/15 text-accent px-4 py-2 text-sm">{actionItems.notice}</p>
      )}

      {shown === 'transcript' ? (
        <Transcript
          utterances={session.utterances ?? []}
          speakers={speakers}
          view={view}
          activeUtteranceId={focusUtteranceId ?? activeUtterance?.utterance_id ?? null}
          onSelect={seekTo}
          onEdit={(utterance, text) => void edit(utterance, text)}
          onRenameSpeaker={(speaker) => void rename(speaker)}
          onActionItem={canExtract ? (utterance) => void extractActionItem(utterance) : undefined}
          actionItemBusyId={actionItems.busyId}
          actionItemSources={actionItems.sources}
          emptyTitle="This session has no transcript."
          emptyAction={session.state === 'processing' ? 'Still processing…' : undefined}
        />
      ) : idlePlugin ? (
        <GeneratePanel
          plugin={idlePlugin}
          onDemand={Boolean(plugins.find((p) => p.name === idlePlugin)?.on_demand)}
          running={rerunning === idlePlugin}
          onGenerate={() => void rerun(idlePlugin)}
        />
      ) : (
        <ArtifactPanel
          artifacts={current.filter((a) => a.kind === shown)}
          versions={(session.artifacts ?? []).filter((a) => a.kind === shown)}
          rerunning={rerunning}
          onRerun={(plugin) => void rerun(plugin)}
        />
      )}

      {session.has_audio && (
        <div className="border-line bg-surface-1 border-t px-4 py-2 pb-[max(0.5rem,env(safe-area-inset-bottom))]">
          <audio
            ref={audio}
            src={api.audioUrl(sessionId)}
            preload="metadata"
            onTimeUpdate={(event) => setPositionMs(event.currentTarget.currentTime * 1000)}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            className="hidden"
          />
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => (playing ? audio.current?.pause() : void audio.current?.play())}
              className="text-xl"
              aria-label={playing ? 'Pause' : 'Play'}
            >
              {playing ? '❚❚' : '▶'}
            </button>
            <input
              type="range"
              min={0}
              max={session.audio_duration_ms ?? session.duration_ms}
              value={positionMs}
              onChange={(event) => {
                const ms = Number(event.target.value)
                setPositionMs(ms)
                if (audio.current) audio.current.currentTime = ms / 1000
              }}
              className="accent-accent flex-1"
              aria-label="Playback position"
            />
            <span className="text-fg-dim font-mono text-xs tabular-nums">
              {clock(positionMs)} / {clock(session.audio_duration_ms ?? session.duration_ms)}
            </span>
          </div>
        </div>
      )}

      {/* The same dialog the history list uses, so a destructive action does
          not behave differently depending on where it was reached from. */}
      {confirmDelete && (
        <ConfirmDialog
          title={strings.history.deleteTitle}
          subject={session.title ?? strings.history.untitled}
          detail={strings.history.deleteDetail}
          confirmLabel={strings.session.delete}
          onConfirm={() => void api.deleteSession(sessionId).then(onBack)}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
    </div>
  )
}

/**
 * The tab for a plugin that has not run on this session.
 *
 * Without it, a recording whose plugins were disabled at the time — or that
 * ended before one was installed — offers no way to produce anything at all,
 * because the tabs were built from artifacts that already existed. For an
 * on-demand plugin it is not a gap to be explained but the normal state, and
 * the wording says so rather than implying something failed.
 */
function GeneratePanel({
  plugin,
  onDemand,
  running,
  onGenerate,
}: {
  plugin: string
  onDemand: boolean
  running: boolean
  onGenerate: () => void
}) {
  const strings = t()
  const label = plugin.replace(/_/g, ' ')
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 px-6 py-16 text-center">
      <p className="text-fg-dim max-w-sm text-sm">
        {onDemand ? strings.session.onDemandHint(label) : strings.session.generateHint(label)}
      </p>
      <Button variant="default" onClick={onGenerate} disabled={running}>
        {running ? strings.session.generating : strings.session.generate(label)}
      </Button>
    </div>
  )
}

function ArtifactPanel({
  artifacts,
  versions,
  rerunning,
  onRerun,
}: {
  artifacts: Artifact[]
  versions: Artifact[]
  rerunning: string | null
  onRerun: (plugin: string) => void
}) {
  const strings = t()
  const [copied, setCopied] = useState(false)
  const artifact = artifacts[0]

  if (!artifact) {
    return <EmptyState title={strings.session.noArtifacts} action={strings.session.noArtifactsAction} />
  }

  // FR-UI-19: the commonest action should not be a file download.
  const copy = async () => {
    await navigator.clipboard.writeText(artifact.content)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 2000)
  }

  const share = async () => {
    if (navigator.share) {
      await navigator.share({ title: artifact.kind, text: artifact.content }).catch(() => undefined)
    }
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-3">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Pill tone="neutral">
          {artifact.plugin_name} v{artifact.version}
        </Pill>
        {versions.length > 1 && <Pill tone="neutral">{versions.length} versions</Pill>}
        <div className="ml-auto flex gap-2">
          <Button variant="ghost" onClick={() => void copy()}>
            {copied ? strings.session.copied : strings.session.copy}
          </Button>
          {'share' in navigator && (
            <Button variant="ghost" onClick={() => void share()}>
              {strings.session.share}
            </Button>
          )}
          <Button variant="default" onClick={() => onRerun(artifact.plugin_name)} disabled={rerunning !== null}>
            {rerunning === artifact.plugin_name ? strings.session.rerunning : strings.session.rerun}
          </Button>
        </div>
      </div>
      <Markdown source={artifact.content} />
    </div>
  )
}
