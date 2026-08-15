/**
 * Confirmation for a destructive action (SRS §5.1, cross-cutting).
 *
 * The rule the SRS states is that deleting a session must be confirmed by a
 * dialog *naming the session*, because nothing is recoverable afterwards. That
 * only works if the name is impossible to leave out, so it is a required prop
 * rather than part of a free-text message.
 */

import { useEffect, useRef } from 'react'
import { t } from '../i18n'

export function ConfirmDialog({
  title,
  subject,
  detail,
  confirmLabel,
  onConfirm,
  onCancel,
  busy = false,
}: {
  title: string
  /** The thing being acted on. Always rendered, always quoted. */
  subject: string
  detail?: string
  confirmLabel: string
  onConfirm: () => void
  onCancel: () => void
  busy?: boolean
}) {
  const strings = t()
  const cancelRef = useRef<HTMLButtonElement>(null)

  // Focus lands on Cancel, not on the destructive action: a stray Enter should
  // not delete a meeting.
  useEffect(() => {
    cancelRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onCancel])

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-4 sm:items-center"
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel()
      }}
    >
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        className="bg-surface-1 border-line w-full max-w-sm rounded-2xl border p-5 shadow-2xl"
      >
        <h2 id="confirm-title" className="mb-2 text-base font-semibold">
          {title}
        </h2>
        <p className="text-fg-dim text-sm break-words">
          “<span className="text-fg font-medium">{subject}</span>”
        </p>
        {detail && <p className="text-fg-dim mt-2 text-sm">{detail}</p>}

        <div className="mt-5 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="hover:bg-surface-2 rounded-xl px-4 py-2.5 text-sm"
          >
            {strings.common.cancel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="bg-danger rounded-xl px-4 py-2.5 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-50"
          >
            {busy ? strings.common.loading : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
