/**
 * Turning one transcript line into an action item.
 *
 * Shared by the two places a transcript is shown, because the feature is only
 * half useful in one of them: a commitment is made *during* a conversation, and
 * being able to catch it as it goes past is the point. Making it a hook rather
 * than a copied handler keeps the availability check, the busy state, and the
 * marks on the lines from drifting apart between the live view and the stored
 * one.
 */

import { useCallback, useEffect, useState } from 'react'
import { api, type Artifact, type Utterance } from '../api/client'
import { t } from '../i18n'

const ACTION_ITEMS = 'action_items'

/**
 * Which lines have already been turned into an action item.
 *
 * Read out of the list itself rather than remembered per click, so the marks
 * survive a reload. Items from a whole-session pass carry no source and mark
 * nothing — correctly: nobody picked those lines.
 */
export function sourcesOf(artifacts: Artifact[]): ReadonlySet<string> {
  const json = artifacts.find((a) => a.kind === 'action_items_json' && a.current !== false)
  if (!json) return new Set()
  try {
    const parsed: unknown = JSON.parse(json.content)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(
      parsed
        .map((item) => (item as { source?: unknown } | null)?.source)
        .filter((source): source is string => typeof source === 'string'),
    )
  } catch {
    // A malformed list costs the marks, not the transcript.
    return new Set()
  }
}

export interface ActionItems {
  /** The rendered list, so a view can show it without fetching again. */
  list: string | null
  /** How many are on it. */
  count: number
  /** False where the plugin is off or has no LLM to reach — the control is a
   *  lie there, so the caller renders nothing rather than a failing button. */
  available: boolean
  /** The line a run is in flight for. */
  busyId: string | null
  sources: ReadonlySet<string>
  /** What happened, in words, or null. Truthful about finding nothing. */
  notice: string | null
  clearNotice: () => void
  extract: (utterance: Utterance) => Promise<void>
  error: string | null
}

export function useActionItems(sessionId: string | null): ActionItems {
  const strings = t()
  const [available, setAvailable] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [sources, setSources] = useState<ReadonlySet<string>>(new Set())
  const [list, setList] = useState<string | null>(null)
  const [count, setCount] = useState(0)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .plugins()
      .then((body) =>
        setAvailable(body.plugins.some((p) => p.name === ACTION_ITEMS && p.enabled && p.available)),
      )
      .catch(() => setAvailable(false))
  }, [])

  // The marks come from the stored list, so they are right on first paint of a
  // session someone already worked through.
  const refresh = useCallback(async () => {
    if (!sessionId) return
    const body = await api.artifacts(sessionId).catch(() => null)
    if (!body) return
    setSources(sourcesOf(body.artifacts))
    const rendered = body.artifacts.find((a) => a.kind === 'action_items' && a.current !== false)
    setList(rendered?.content ?? null)
    setCount(Number(rendered?.metadata?.count ?? 0))
  }, [sessionId])

  useEffect(() => {
    if (!sessionId) {
      setSources(new Set())
      setList(null)
      setCount(0)
      return
    }
    void refresh()
  }, [sessionId, refresh])

  const extract = useCallback(
    async (utterance: Utterance) => {
      if (!sessionId) return
      setBusyId(utterance.utterance_id)
      setNotice(null)
      setError(null)
      try {
        const result = await api.runPlugin(sessionId, ACTION_ITEMS, {
          utteranceIds: [utterance.utterance_id],
        })
        // Three outcomes, and a reader acts on each differently. Saying "added"
        // for a line holding no commitment sends them to a tab that does not
        // contain what they were promised; saying "nothing to act on" about a
        // line that is already a task is simply untrue.
        const added = Number(result.artifact?.metadata?.added ?? 0)
        const linked = Number(result.artifact?.metadata?.linked ?? 0)
        setNotice(
          added > 0
            ? strings.session.actionItemAdded(added)
            : linked > 0
              ? strings.session.actionItemLinked
              : strings.session.actionItemNothing,
        )
        if (added > 0 || linked > 0) await refresh()
      } catch (thrown) {
        setError(thrown instanceof Error ? thrown.message : strings.errors.generic)
      } finally {
        setBusyId(null)
      }
    },
    [sessionId, refresh, strings.session, strings.errors.generic],
  )

  return {
    list,
    count,
    available,
    busyId,
    sources,
    notice,
    clearNotice: useCallback(() => setNotice(null), []),
    extract,
    error,
  }
}
