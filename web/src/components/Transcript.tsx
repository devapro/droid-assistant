/**
 * The transcript list — shared by the live view and the session detail view.
 *
 * Three behaviours here are specified rather than incidental:
 *
 * * **Autoscroll is pinned to the newest utterance until the user scrolls up**
 *   (FR-UI-13). Arriving text must never yank the viewport out from under
 *   someone reading back, so scrolling away releases the pin and reveals an
 *   explicit control to restore it.
 * * **Partials are visibly provisional** — italic, with a caret — and carry no
 *   speaker, because diarization needs a completed segment (FR-DIA-10).
 * * **A translation that has not arrived is *pending*, not empty and not
 *   failed** (FR-UI-14). Those are three different states and the user can act
 *   on only one of them.
 *
 * Rendering is windowed above a threshold so a 2000-utterance session scrolls
 * without blocking interaction (NFR-PERF-8).
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { Speaker, Utterance } from '../api/client'
import { t } from '../i18n'
import { clock, speakerColour } from './primitives'

export type TranscriptView = 'original' | 'translation' | 'both'

interface Props {
  utterances: Utterance[]
  partial?: Utterance | null
  speakers: Record<string, Speaker>
  view?: TranscriptView
  /** Live sessions pin to the newest line; a stored session does not. */
  autoscroll?: boolean
  activeUtteranceId?: string | null
  onSelect?: (utterance: Utterance) => void
  onEdit?: (utterance: Utterance, text: string) => void
  onRenameSpeaker?: (speaker: Speaker) => void
  emptyTitle?: string
  emptyAction?: string
}

/** Above this many lines, render a window rather than the whole list. */
const WINDOW_THRESHOLD = 200
const WINDOW_SIZE = 120

export function Transcript({
  utterances,
  partial,
  speakers,
  view = 'both',
  autoscroll = false,
  activeUtteranceId,
  onSelect,
  onEdit,
  onRenameSpeaker,
  emptyTitle,
  emptyAction,
}: Props) {
  const strings = t()
  const scroller = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')

  const onScroll = useCallback(() => {
    const element = scroller.current
    if (!element) return
    // 48 px of slack: a pin that releases on a one-pixel overscroll is worse
    // than no pin at all.
    const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 48
    setPinned(atBottom)
  }, [])

  useLayoutEffect(() => {
    if (!autoscroll || !pinned) return
    const element = scroller.current
    if (element) element.scrollTop = element.scrollHeight
  }, [utterances, partial, autoscroll, pinned])

  useEffect(() => {
    if (!activeUtteranceId) return
    const node = scroller.current?.querySelector(`[data-utt="${activeUtteranceId}"]`)
    node?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [activeUtteranceId])

  const visible = useMemo(() => {
    if (utterances.length <= WINDOW_THRESHOLD) return utterances
    // Live sessions read from the bottom; a stored one is browsed from an
    // anchor. Either way we keep a window rather than mounting 2000 nodes.
    return utterances.slice(-WINDOW_SIZE)
  }, [utterances])

  const truncated = utterances.length - visible.length

  if (utterances.length === 0 && !partial) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 py-16 text-center">
        <p className="text-fg font-medium">{emptyTitle ?? strings.record.emptyTranscript}</p>
        {emptyAction && <p className="text-fg-dim max-w-xs text-sm">{emptyAction}</p>}
      </div>
    )
  }

  const commitEdit = (utterance: Utterance) => {
    if (draft.trim() && draft !== utterance.text) onEdit?.(utterance, draft.trim())
    setEditing(null)
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={scroller} onScroll={onScroll} className="flex-1 overflow-y-auto overscroll-contain px-3 py-2">
        {truncated > 0 && (
          <p className="text-fg-dim py-3 text-center text-xs">
            {truncated} earlier line{truncated === 1 ? '' : 's'} not shown
          </p>
        )}

        {visible.map((utterance) => {
          const speaker = utterance.speaker_id ? speakers[utterance.speaker_id] : undefined
          const colour = speakerColour(speaker?.index)
          const isEditing = editing === utterance.utterance_id
          return (
            <article
              key={utterance.utterance_id}
              data-utt={utterance.utterance_id}
              className={`group mb-3 border-l-2 pl-3 transition-colors ${
                activeUtteranceId === utterance.utterance_id ? 'bg-accent/10 rounded-r' : ''
              }`}
              style={{ borderColor: colour.bar }}
            >
              <header className="mb-0.5 flex items-baseline gap-2">
                {/* Colour is never the only carrier of identity (FR-UI-16). */}
                <button
                  type="button"
                  onClick={() => speaker && onRenameSpeaker?.(speaker)}
                  disabled={!speaker || !onRenameSpeaker}
                  className="text-sm font-semibold enabled:hover:underline"
                  style={{ color: colour.text }}
                >
                  {speaker?.name ?? strings.record.unknownSpeaker}
                </button>
                <button
                  type="button"
                  onClick={() => onSelect?.(utterance)}
                  className="text-fg-dim hover:text-fg font-mono text-xs tabular-nums"
                >
                  {clock(utterance.start_ms)}
                </button>
                {utterance.marked && <span title="Marked moment">⚑</span>}
                {utterance.edited && <span className="text-fg-dim text-xs">{strings.session.edited}</span>}
                {onEdit && !isEditing && (
                  <button
                    type="button"
                    onClick={() => {
                      setEditing(utterance.utterance_id)
                      setDraft(utterance.text)
                    }}
                    className="text-fg-dim hover:text-fg ml-auto text-xs opacity-0 transition group-hover:opacity-100 focus:opacity-100"
                    aria-label="Edit this line"
                  >
                    ✎
                  </button>
                )}
              </header>

              {isEditing ? (
                <div className="flex flex-col gap-2">
                  <textarea
                    value={draft}
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        commitEdit(utterance)
                      }
                      if (event.key === 'Escape') setEditing(null)
                    }}
                    rows={2}
                    autoFocus
                    className="border-line bg-surface-2 focus:border-accent w-full rounded-lg border px-2 py-1 text-[15px] outline-none"
                  />
                  <div className="flex gap-2 text-xs">
                    <button type="button" onClick={() => commitEdit(utterance)} className="text-accent">
                      {strings.common.save}
                    </button>
                    <button type="button" onClick={() => setEditing(null)} className="text-fg-dim">
                      {strings.common.cancel}
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  {view !== 'translation' && (
                    <p className="text-[15px] leading-snug break-words">{utterance.text}</p>
                  )}
                  {view !== 'original' && <TranslationLine utterance={utterance} />}
                </>
              )}
            </article>
          )
        })}

        {partial && (
          <article className="border-fg-dim/30 mb-3 border-l-2 pl-3">
            <header className="mb-0.5 flex items-baseline gap-2">
              {/* A partial has no speaker: guessing and then flipping the label
                  would be worse than showing nothing (FR-DIA-10). */}
              <span className="text-fg-dim text-sm font-semibold">{strings.record.unknownSpeaker}</span>
              <span className="text-fg-dim font-mono text-xs tabular-nums">{clock(partial.start_ms)}</span>
              <span className="sr-only">{strings.record.partialHint}</span>
            </header>
            <p className="text-fg-dim text-[15px] leading-snug italic break-words">
              {partial.text}
              <span className="bg-accent ml-0.5 inline-block h-4 w-0.5 animate-pulse align-text-bottom" />
            </p>
          </article>
        )}
      </div>

      {autoscroll && !pinned && (
        <button
          type="button"
          onClick={() => {
            setPinned(true)
            const element = scroller.current
            if (element) element.scrollTop = element.scrollHeight
          }}
          className="bg-accent absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full px-3 py-1.5 text-sm font-medium text-white shadow-lg"
        >
          ↓ {strings.record.jumpToLive}
        </button>
      )}
    </div>
  )
}

/** FR-UI-14: pending and failed are visibly different states. */
function TranslationLine({ utterance }: { utterance: Utterance }) {
  const strings = t()
  if (utterance.translation_state === 'skipped' || utterance.translation_state === 'none') return null
  if (utterance.translation_state === 'pending') {
    return (
      <p className="text-fg-dim/70 mt-0.5 text-sm italic">
        <span className="bg-fg-dim/40 mr-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full" />
        {strings.record.translationPending}
      </p>
    )
  }
  if (utterance.translation_state === 'failed') {
    return <p className="text-warn mt-0.5 text-sm">⚠ {strings.record.translationFailed}</p>
  }
  return <p className="text-fg-dim mt-0.5 text-sm leading-snug break-words">{utterance.translation}</p>
}
