/**
 * Screen 2 — History (SRS §5.1).
 *
 * Each row carries what actually distinguishes one conversation from another:
 * when, how long, how many speakers, the language pair, and which artifacts
 * exist. Duration and speaker count do more work than the title, because
 * auto-generated titles are often weak.
 *
 * Searching switches the list to *matches* rather than sessions (FR-SES-10),
 * and selecting one opens the session scrolled to that utterance rather than to
 * the top.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, api, type SearchHit, type Session } from '../api/client'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { EmptyState, Pill, duration, relativeDate } from '../components/primitives'
import { t } from '../i18n'

/**
 * How many sessions a page holds. Large enough that most people never reach the
 * end of the first one, small enough that the request stays quick — each row
 * costs three extra queries on the server for speaker and artifact counts.
 */
const PAGE_SIZE = 50

export function History({ onOpen }: { onOpen: (sessionId: string, utteranceId?: string) => void }) {
  const strings = t()
  const [sessions, setSessions] = useState<Session[]>([])
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [query, setQuery] = useState('')
  const [language, setLanguage] = useState('')
  const [tag, setTag] = useState('')
  const [range, setRange] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [facets, setFacets] = useState<{ languages: string[]; tags: string[] }>({
    languages: [],
    tags: [],
  })
  const sentinel = useRef<HTMLButtonElement | null>(null)
  const [renamingId, setRenamingId] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<Session | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; remedy?: string } | null>(null)

  const fail = (thrown: unknown) => {
    const problem = thrown instanceof ApiError ? thrown : null
    setError({
      message: problem?.message ?? strings.errors.generic,
      remedy: problem?.remedy,
    })
  }

  // Both actions update the list in place rather than refetching, so the
  // filters and the scroll position the user set up survive them.
  const rename = async (session: Session, title: string) => {
    const trimmed = title.trim()
    setRenamingId(null)
    if (!trimmed || trimmed === session.title) return
    const previous = session.title
    setSessions((current) =>
      current.map((s) => (s.id === session.id ? { ...s, title: trimmed } : s)),
    )
    try {
      setError(null)
      await api.updateSession(session.id, { title: trimmed })
    } catch (thrown) {
      // Put the old name back rather than leaving the list claiming something
      // the server does not agree with.
      setSessions((current) =>
        current.map((s) => (s.id === session.id ? { ...s, title: previous } : s)),
      )
      fail(thrown)
    }
  }

  const remove = async (session: Session) => {
    setBusy(true)
    try {
      setError(null)
      await api.deleteSession(session.id)
      setSessions((current) => current.filter((s) => s.id !== session.id))
      // Keep the count honest — it is what "Load more" counts down from.
      setTotal((current) => Math.max(0, current - 1))
      setPendingDelete(null)
      // A deleted session may still be in the current search results.
      setHits((current) => current?.filter((h) => h.session_id !== session.id) ?? null)
    } catch (thrown) {
      fail(thrown)
      setPendingDelete(null)
    } finally {
      setBusy(false)
    }
  }

  const since = useMemo(() => {
    const now = Date.now()
    switch (range) {
      case 'today':
        return new Date(new Date().setHours(0, 0, 0, 0)).getTime()
      case 'week':
        return now - 7 * 86_400_000
      case 'month':
        return now - 30 * 86_400_000
      default:
        return undefined
    }
  }, [range])

  // The list used to fetch a flat 100 and stop, with nothing saying so — past
  // that, older conversations simply did not exist as far as the UI was
  // concerned. Now it pages, and the header says how many there are.
  const filters = useMemo(
    () => ({ language: language || undefined, tag: tag || undefined, since_ms: since }),
    [language, tag, since],
  )

  // `useCallback` so the effects below can depend on these honestly. Without a
  // stable identity the dependency arrays would either lie or loop.
  const loadPage = useCallback(
    async (offset: number) => {
      const result = await api.listSessions({ ...filters, limit: PAGE_SIZE, offset })
      setTotal(result.total)
      setHasMore(result.has_more)
      setFacets(result.facets)
      // Append by id rather than by index: a rename or delete may have landed
      // between two pages, and concatenating blindly would duplicate a row.
      setSessions((current) => {
        if (offset === 0) return result.sessions
        const seen = new Set(current.map((s) => s.id))
        return [...current, ...result.sessions.filter((s) => !seen.has(s.id))]
      })
    },
    [filters],
  )

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void (async () => {
      try {
        if (!cancelled) await loadPage(0)
      } catch {
        if (!cancelled) {
          setSessions([])
          setTotal(0)
          setHasMore(false)
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [filters, loadPage])

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return
    setLoadingMore(true)
    try {
      setError(null)
      await loadPage(sessions.length)
    } catch (thrown) {
      fail(thrown)
    } finally {
      setLoadingMore(false)
    }
    // `fail` closes over nothing that changes; the rest is genuine state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadingMore, hasMore, sessions.length, loadPage])

  // Fetch the next page as the end of the list comes into view. The button is
  // the sentinel, so it still works by click — and by keyboard — if the
  // observer never fires.
  useEffect(() => {
    const node = sentinel.current
    if (!node || !hasMore) return
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadMore()
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [hasMore, loadMore])

  // Debounced, because this runs FTS5 across every session on each keystroke.
  useEffect(() => {
    if (!query.trim()) {
      setHits(null)
      return
    }
    const handle = window.setTimeout(() => {
      void api
        .search(query)
        .then((result) => setHits(result.hits))
        .catch(() => setHits([]))
    }, 200)
    return () => window.clearTimeout(handle)
  }, [query])

  return (
    <div className="flex h-full flex-col">
      <header className="border-line border-b px-4 py-3">
        <div className="mb-3 flex items-baseline gap-2">
          <h1 className="text-lg font-semibold">{strings.history.title}</h1>
          {/* Without this the list silently ended at whatever had loaded, and
              there was no way to tell a short history from a truncated one. */}
          {!loading && hits === null && total > 0 && (
            <span className="text-fg-dim text-sm" data-testid="session-count">
              {strings.history.showing(sessions.length, total)}
            </span>
          )}
        </div>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={strings.history.searchPlaceholder}
          type="search"
          className="bg-surface-2 border-line focus:border-accent w-full rounded-xl border px-3 py-2 text-sm outline-none"
        />
        <div className="mt-2 flex flex-wrap gap-2 text-sm">
          <Filter value={range} onChange={setRange} label={strings.history.allTime}
            options={[['today', 'Today'], ['week', 'Last 7 days'], ['month', 'Last 30 days']]} />
          <Filter value={language} onChange={setLanguage} label={strings.history.allLanguages}
            options={facets.languages.map((code) => [code, code.toUpperCase()])} />
          {facets.tags.length > 0 && (
            <Filter value={tag} onChange={setTag} label={strings.history.tags}
              options={facets.tags.map((name) => [name, `#${name}`])} />
          )}
        </div>
      </header>

      {/* FR-UI-9: name the component, say what to do about it. */}
      {error && (
        <div role="alert" className="bg-danger/15 text-danger flex items-start gap-2 px-4 py-2 text-sm">
          <div className="flex-1">
            <p>{error.message}</p>
            {error.remedy && <p className="mt-0.5 text-xs opacity-80">{error.remedy}</p>}
          </div>
          <button type="button" onClick={() => setError(null)} aria-label="Dismiss" className="opacity-60">
            ✕
          </button>
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {hits !== null ? (
          hits.length === 0 ? (
            <EmptyState title={strings.history.noResults(query)} action={strings.history.noResultsAction} icon="🔍" />
          ) : (
            hits.map((hit, index) => (
              <button
                key={`${hit.session_id}-${hit.utterance_id ?? hit.artifact_id ?? index}`}
                type="button"
                onClick={() => onOpen(hit.session_id, hit.utterance_id ?? undefined)}
                className="border-line hover:bg-surface-2 w-full border-b px-4 py-3 text-left"
              >
                <div className="mb-1 flex items-baseline justify-between gap-2">
                  <span className="truncate text-sm font-medium">{hit.session_title ?? 'Untitled'}</span>
                  <span className="text-fg-dim shrink-0 text-xs">{relativeDate(hit.session_started_at)}</span>
                </div>
                <p className="text-fg-dim line-clamp-2 text-sm">
                  <Highlighted snippet={hit.snippet} />
                </p>
                <p className="text-fg-dim mt-1 text-xs">
                  {hit.kind === 'utterance'
                    ? `${hit.speaker ?? strings.common.unknown} · ${formatMs(hit.start_ms ?? 0)}`
                    : `${hit.artifact_kind?.replace('_', ' ')} · ${hit.plugin_name}`}
                </p>
              </button>
            ))
          )
        ) : loading ? (
          <p className="text-fg-dim p-6 text-center text-sm">{strings.common.loading}</p>
        ) : sessions.length === 0 ? (
          <EmptyState title={strings.history.empty} action={strings.history.emptyAction} icon="🎙" />
        ) : (
          <>
            {sessions.map((session) => (
              <SessionRow
                key={session.id}
                session={session}
                editing={renamingId === session.id}
                onOpen={() => onOpen(session.id)}
                onStartRename={() => setRenamingId(session.id)}
                onCancelRename={() => setRenamingId(null)}
                onRename={(title) => void rename(session, title)}
                onDelete={() => setPendingDelete(session)}
              />
            ))}
            {hasMore && (
              <button
                ref={sentinel}
                type="button"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                className="text-fg-dim hover:bg-surface-2 w-full px-4 py-4 text-center text-sm disabled:opacity-60"
              >
                {loadingMore
                  ? strings.common.loading
                  : strings.history.loadMore(total - sessions.length)}
              </button>
            )}
          </>
        )}
      </div>

      {pendingDelete && (
        <ConfirmDialog
          title={strings.history.deleteTitle}
          subject={pendingDelete.title ?? strings.history.untitled}
          detail={
            pendingDelete.live ? strings.history.deleteLiveDetail : strings.history.deleteDetail
          }
          confirmLabel={strings.history.deleteConfirm}
          busy={busy}
          onConfirm={() => void remove(pendingDelete)}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  )
}

/**
 * One session in the list.
 *
 * The whole row used to be a single button. Rename and delete cannot live
 * inside that — a button inside a button is invalid and unreachable by
 * keyboard — so the open target and the actions are siblings.
 */
function SessionRow({
  session,
  editing,
  onOpen,
  onStartRename,
  onCancelRename,
  onRename,
  onDelete,
}: {
  session: Session
  editing: boolean
  onOpen: () => void
  onStartRename: () => void
  onCancelRename: () => void
  onRename: (title: string) => void
  onDelete: () => void
}) {
  const strings = t()
  const name = session.title ?? strings.history.untitled
  const [draft, setDraft] = useState(name)

  useEffect(() => {
    if (editing) setDraft(session.title ?? '')
  }, [editing, session.title])

  if (editing) {
    return (
      <form
        data-session-id={session.id}
        className="border-line flex flex-wrap items-center gap-2 border-b px-4 py-3"
        onSubmit={(event) => {
          event.preventDefault()
          onRename(draft)
        }}
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') onCancelRename()
          }}
          placeholder={strings.history.renamePlaceholder}
          aria-label={strings.history.renameTitle}
          autoFocus
          className="bg-surface-2 border-line focus:border-accent min-w-0 flex-1 rounded-lg border px-3 py-2 text-sm outline-none"
        />
        <div className="ml-auto flex gap-2">
          <button
            type="submit"
            disabled={!draft.trim()}
            className="bg-accent rounded-lg px-3 py-2 text-sm font-medium text-white disabled:opacity-40"
          >
            {strings.common.save}
          </button>
          <button type="button" onClick={onCancelRename} className="text-fg-dim px-3 py-2 text-sm">
            {strings.common.cancel}
          </button>
        </div>
      </form>
    )
  }

  return (
    <div
      data-session-id={session.id}
      className="border-line hover:bg-surface-2 flex items-start border-b transition-colors"
    >
      <button type="button" onClick={onOpen} className="min-w-0 flex-1 px-4 py-3 text-left">
        <div className="mb-1 flex items-baseline gap-2">
          <span className="truncate font-medium">{name}</span>
          {session.live && <Pill tone="bad">{strings.history.live}</Pill>}
        </div>
        <p className="text-fg-dim text-sm">
          {relativeDate(session.started_at)} · {duration(session.duration_ms)} ·{' '}
          {strings.history.speakerCount(session.speaker_count ?? 0)}
        </p>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
          <Pill tone="neutral">
            {(session.source_languages[0] ?? 'auto').toUpperCase()}→
            {session.target_language.toUpperCase()}
          </Pill>
          {session.artifact_kinds?.length ? (
            session.artifact_kinds.map((kind) => (
              <Pill key={kind} tone="accent">
                {kind.replace('_', ' ')}
              </Pill>
            ))
          ) : (
            <span className="text-fg-dim">{strings.history.noArtifacts}</span>
          )}
          {session.cloud_used && <Pill tone="warn">cloud</Pill>}
          {session.tags.map((tag) => (
            <span key={tag} className="text-fg-dim">
              #{tag}
            </span>
          ))}
        </div>
      </button>

      {/* Always visible rather than revealed on hover: there is no hover on a
          phone, and a control you cannot discover is not a control. Delete is
          safe to expose because it is confirmed by name. */}
      <div className="flex shrink-0 items-center gap-0.5 py-3 pr-2">
        <button
          type="button"
          onClick={onStartRename}
          aria-label={strings.history.rename(name)}
          title={strings.history.renameTitle}
          className="text-fg-dim hover:bg-surface-3 hover:text-fg flex h-10 w-10 items-center justify-center rounded-lg"
        >
          ✎
        </button>
        <button
          type="button"
          onClick={onDelete}
          aria-label={strings.history.delete(name)}
          title={strings.history.deleteConfirm}
          className="text-fg-dim hover:bg-danger/15 hover:text-danger flex h-10 w-10 items-center justify-center rounded-lg"
        >
          ✕
        </button>
      </div>
    </div>
  )
}

function Filter({
  value,
  onChange,
  label,
  options,
}: {
  value: string
  onChange: (value: string) => void
  label: string
  options: [string, string][]
}) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className="bg-surface-2 border-line rounded-lg border px-2 py-1"
      aria-label={label}
    >
      <option value="">{label}</option>
      {options.map(([key, text]) => (
        <option key={key} value={key}>
          {text}
        </option>
      ))}
    </select>
  )
}

/** The server marks matches with ⟦…⟧; render those as highlights. */
function Highlighted({ snippet }: { snippet: string }) {
  const parts = snippet.split(/(⟦[^⟧]*⟧)/g)
  return (
    <>
      {parts.map((part, index) =>
        part.startsWith('⟦') ? (
          <mark key={index} className="bg-accent/25 text-fg rounded px-0.5">
            {part.slice(1, -1)}
          </mark>
        ) : (
          <span key={index}>{part}</span>
        ),
      )}
    </>
  )
}

function formatMs(ms: number): string {
  const seconds = Math.floor(ms / 1000)
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}
