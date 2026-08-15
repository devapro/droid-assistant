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
import { api, type Artifact, type Session, type Speaker, type Utterance } from '../api/client'
import { EventStream } from '../transport/events'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { CostBreakdown } from '../components/CostBreakdown'
import { Transcript, type TranscriptView } from '../components/Transcript'
import { Button, EmptyState, Pill, clock, duration, relativeDate } from '../components/primitives'
import { t } from '../i18n'

type Tab = 'transcript' | string

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
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const audio = useRef<HTMLAudioElement>(null)

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
    try {
      await api.runPlugin(sessionId, pluginName)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : strings.errors.generic)
    } finally {
      setRerunning(null)
    }
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
  const tabs: { id: Tab; label: string }[] = [
    { id: 'transcript', label: strings.session.transcript },
    ...current.map((a) => ({ id: a.kind, label: a.kind.replace(/_/g, ' ') })),
  ]

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
        {tab === 'transcript' && session.target_language && (
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

      {session.artifacts_stale && tab !== 'transcript' && (
        <p className="bg-warn/15 text-warn px-4 py-2 text-sm">{strings.session.stale}</p>
      )}

      {tab === 'transcript' ? (
        <Transcript
          utterances={session.utterances ?? []}
          speakers={speakers}
          view={view}
          activeUtteranceId={focusUtteranceId ?? activeUtterance?.utterance_id ?? null}
          onSelect={seekTo}
          onEdit={(utterance, text) => void edit(utterance, text)}
          onRenameSpeaker={(speaker) => void rename(speaker)}
          emptyTitle="This session has no transcript."
          emptyAction={session.state === 'processing' ? 'Still processing…' : undefined}
        />
      ) : (
        <ArtifactPanel
          artifacts={current.filter((a) => a.kind === tab)}
          versions={(session.artifacts ?? []).filter((a) => a.kind === tab)}
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

/**
 * A small Markdown renderer for artifact content.
 *
 * A full parser would be the single largest thing in the bundle (NFR-RES-6), and
 * artifacts come from prompts that ask for headings, lists, and emphasis. Text
 * is never injected as HTML, so a malformed artifact renders as plain text
 * rather than becoming a script (NFR-SEC-9).
 */
function Markdown({ source }: { source: string }) {
  const lines = source.split('\n')
  return (
    <div className="max-w-prose text-[15px] leading-relaxed">
      {lines.map((line, index) => {
        if (/^#{1,6}\s/.test(line)) {
          const level = line.match(/^#+/)?.[0].length ?? 1
          const text = line.replace(/^#+\s*/, '')
          const size = level <= 2 ? 'text-lg font-semibold' : 'text-base font-semibold'
          return (
            <p key={index} className={`mt-4 mb-1 ${size}`}>
              {inline(text)}
            </p>
          )
        }
        if (/^\s*[-*]\s*\[[ x]\]\s/.test(line)) {
          const done = /\[x\]/i.test(line)
          return (
            <p key={index} className="flex gap-2 py-0.5">
              <span className={done ? 'text-good' : 'text-fg-dim'}>{done ? '☑' : '☐'}</span>
              <span>{inline(line.replace(/^\s*[-*]\s*\[[ x]\]\s*/i, ''))}</span>
            </p>
          )
        }
        if (/^\s*[-*]\s/.test(line)) {
          return (
            <p key={index} className="flex gap-2 py-0.5 pl-2">
              <span className="text-fg-dim">•</span>
              <span>{inline(line.replace(/^\s*[-*]\s*/, ''))}</span>
            </p>
          )
        }
        if (/^\s*>/.test(line)) {
          return (
            <p key={index} className="border-line text-fg-dim my-1 border-l-2 pl-3 text-sm italic">
              {inline(line.replace(/^\s*>\s?/, ''))}
            </p>
          )
        }
        if (!line.trim()) return <div key={index} className="h-2" />
        return (
          <p key={index} className="py-0.5">
            {inline(line)}
          </p>
        )
      })}
    </div>
  )
}

function inline(text: string): React.ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|_[^_]+_|`[^`]+`)/g)
  return parts.map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('_') && part.endsWith('_') && part.length > 2) {
      return <em key={index}>{part.slice(1, -1)}</em>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return (
        <code key={index} className="bg-surface-2 rounded px-1 py-0.5 font-mono text-[13px]">
          {part.slice(1, -1)}
        </code>
      )
    }
    return <span key={index}>{part}</span>
  })
}
