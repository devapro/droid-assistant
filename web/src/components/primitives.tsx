/** Shared presentational pieces used across the four screens. */

import type { ReactNode } from 'react'

// FR-UI-16: a fixed, colourblind-safe ordered palette (Okabe–Ito), stable per
// speaker index everywhere in the app. Colour is never the only carrier of
// identity — every utterance also shows a textual label.
export const SPEAKER_COLOURS = [
  { bar: '#0072B2', text: '#3AA3E3', name: 'blue' },
  { bar: '#E69F00', text: '#E69F00', name: 'orange' },
  { bar: '#009E73', text: '#00BE8C', name: 'green' },
  { bar: '#CC79A7', text: '#E093BE', name: 'pink' },
  { bar: '#56B4E9', text: '#56B4E9', name: 'sky' },
  { bar: '#D55E00', text: '#F07032', name: 'vermillion' },
  { bar: '#F0E442', text: '#C9BC10', name: 'yellow' },
  { bar: '#8C8C8C', text: '#A6A6A6', name: 'grey' },
] as const

export function speakerColour(index: number | undefined): (typeof SPEAKER_COLOURS)[number] {
  if (index === undefined || index < 0) return SPEAKER_COLOURS[7]!
  return SPEAKER_COLOURS[index % SPEAKER_COLOURS.length]!
}

export function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const seconds = total % 60
  const minutes = Math.floor(total / 60) % 60
  const hours = Math.floor(total / 3600)
  const pad = (n: number) => String(n).padStart(2, '0')
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${pad(minutes)}:${pad(seconds)}`
}

export function relativeDate(ms: number): string {
  const date = new Date(ms)
  const today = new Date()
  const isToday = date.toDateString() === today.toDateString()
  const yesterday = new Date(today.getTime() - 86_400_000)
  const isYesterday = date.toDateString() === yesterday.toDateString()
  const time = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (isToday) return `Today ${time}`
  if (isYesterday) return `Yesterday ${time}`
  return `${date.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })} ${time}`
}

export function duration(ms: number): string {
  const minutes = Math.round(ms / 60_000)
  if (minutes < 1) return `${Math.max(1, Math.round(ms / 1000))} s`
  if (minutes < 60) return `${minutes} min`
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`
}

export function Button({
  children,
  onClick,
  variant = 'default',
  disabled,
  className = '',
  type = 'button',
  ariaLabel,
}: {
  children: ReactNode
  onClick?: () => void
  variant?: 'default' | 'primary' | 'danger' | 'ghost' | 'record' | 'stop'
  disabled?: boolean
  className?: string
  type?: 'button' | 'submit'
  ariaLabel?: string
}) {
  const styles: Record<string, string> = {
    default: 'bg-surface-2 hover:bg-surface-3 text-fg border border-line',
    primary: 'bg-accent hover:brightness-110 text-white',
    danger: 'bg-danger hover:brightness-110 text-white',
    ghost: 'hover:bg-surface-2 text-fg-dim hover:text-fg',
    record: 'bg-danger hover:brightness-110 text-white text-lg font-semibold',
    stop: 'bg-surface-3 hover:bg-surface-2 text-fg border border-line text-lg font-semibold',
  }
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      className={`rounded-xl px-4 py-2.5 transition disabled:cursor-not-allowed disabled:opacity-40 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${styles[variant]} ${className}`}
    >
      {children}
    </button>
  )
}

/**
 * FR-UI-15: every list and search view defines an empty state that says what to
 * do next. A blank area reads as a bug; this reads as an invitation.
 */
export function EmptyState({
  title,
  action,
  icon,
}: {
  title: string
  action?: string
  icon?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-16 text-center">
      {icon && <div className="mb-2 text-4xl opacity-40">{icon}</div>}
      <p className="text-fg font-medium">{title}</p>
      {action && <p className="text-fg-dim max-w-xs text-sm">{action}</p>}
    </div>
  )
}

/** FR-CAP-10: a resting meter in a silent room, moving within 200 ms of speech. */
export function LevelMeter({ level, className = '' }: { level: number; className?: string }) {
  const bars = 12
  // Log scaling: linear amplitude puts normal speech in the bottom fifth of the
  // meter, which makes a working microphone look broken.
  const scaled = level > 0 ? Math.min(1, Math.max(0, (20 * Math.log10(level) + 60) / 60)) : 0
  const lit = Math.round(scaled * bars)
  return (
    <div
      className={`flex items-end gap-0.5 ${className}`}
      role="meter"
      aria-valuenow={Math.round(scaled * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="Input level"
    >
      {Array.from({ length: bars }, (_, i) => (
        <span
          key={i}
          className="w-1 rounded-full transition-[height,background-color] duration-75"
          style={{
            height: `${6 + i * 1.4}px`,
            backgroundColor:
              i < lit ? (i > bars - 3 ? 'var(--color-danger)' : 'var(--color-accent)') : 'var(--color-line)',
          }}
        />
      ))}
    </div>
  )
}

export function Pill({
  children,
  tone = 'neutral',
}: {
  children: ReactNode
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'accent'
}) {
  const tones: Record<string, string> = {
    neutral: 'bg-surface-2 text-fg-dim',
    good: 'bg-good/15 text-good',
    warn: 'bg-warn/15 text-warn',
    bad: 'bg-danger/15 text-danger',
    accent: 'bg-accent/15 text-accent',
  }
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${tones[tone]}`}>{children}</span>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="text-fg-dim inline-flex items-center gap-2 text-sm">
      <span className="border-fg-dim/30 border-t-accent h-3.5 w-3.5 animate-spin rounded-full border-2" />
      {label}
    </span>
  )
}
