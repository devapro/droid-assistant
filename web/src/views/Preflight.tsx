/**
 * Pre-flight check (FR-UI-18).
 *
 * Two failures cost a whole meeting and are invisible until it is too late: the
 * server is not there, and the microphone is not picking anything up. Both are
 * checked in about a second before recording starts, and neither is a dead end
 * — an unreachable server offers offline recording (FR-CAP-17), and a silent
 * microphone can be overridden if the user knows better than we do.
 */

import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { LevelMeter } from '../components/primitives'
import { t } from '../i18n'
import type { CaptureSettings } from '../state/recording'

export interface PreflightResult {
  action: 'record' | 'offline' | 'cancel'
}

type CheckState = 'pending' | 'pass' | 'fail'

const LISTEN_MS = 1500
/** Below this peak over the listening window, treat the microphone as silent. */
const SILENCE_PEAK = 0.005

export function PreflightDialog({
  settings,
  onDone,
}: {
  settings: CaptureSettings
  onDone: (result: PreflightResult) => void
}) {
  const strings = t()
  const [server, setServer] = useState<CheckState>('pending')
  const [serverMessage, setServerMessage] = useState('')
  const [mic, setMic] = useState<CheckState>('pending')
  const [micMessage, setMicMessage] = useState('')
  const [peak, setPeak] = useState(0)
  const cleanup = useRef<(() => void) | null>(null)

  useEffect(() => {
    let cancelled = false

    void (async () => {
      try {
        const health = await api.health()
        if (cancelled) return
        if (health.disk.below_minimum) {
          setServer('fail')
          setServerMessage(strings.preflight.diskFull)
        } else {
          setServer('pass')
          setServerMessage(health.disk.low ? strings.preflight.diskLow : strings.preflight.serverOk)
        }
      } catch {
        if (cancelled) return
        setServer('fail')
        setServerMessage(strings.preflight.serverFail)
      }
    })()

    void (async () => {
      try {
        // Display capture opens a picker, so it is not probed here: asking the
        // user to choose a tab twice — once to check, once to record — is worse
        // than starting and reporting a silent share immediately.
        if (settings.source === 'system') {
          setMic('pass')
          setMicMessage(strings.preflight.sourcePending)
          return
        }
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            deviceId: settings.deviceId ? { exact: settings.deviceId } : undefined,
            echoCancellation: settings.echoCancellation,
            noiseSuppression: settings.noiseSuppression,
            autoGainControl: settings.autoGainControl,
          },
        })
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop())
          return
        }
        const context = new AudioContext()
        const analyser = context.createAnalyser()
        analyser.fftSize = 1024
        context.createMediaStreamSource(stream).connect(analyser)
        const samples = new Float32Array(analyser.fftSize)
        let observed = 0
        let frame = 0

        const tick = () => {
          analyser.getFloatTimeDomainData(samples)
          let localPeak = 0
          for (const value of samples) localPeak = Math.max(localPeak, Math.abs(value))
          observed = Math.max(observed, localPeak)
          setPeak(localPeak)
          frame = requestAnimationFrame(tick)
        }
        tick()

        let released = false
        cleanup.current = () => {
          if (released) return
          released = true
          cancelAnimationFrame(frame)
          stream.getTracks().forEach((track) => track.stop())
          void context.close().catch(() => undefined)
        }

        window.setTimeout(() => {
          if (cancelled) return
          if (observed < SILENCE_PEAK) {
            setMic('fail')
            setMicMessage(strings.preflight.micSilent)
          } else {
            setMic('pass')
            setMicMessage(strings.preflight.micOk)
          }
        }, LISTEN_MS)
      } catch {
        if (cancelled) return
        setMic('fail')
        setMicMessage(strings.preflight.micFail)
      }
    })()

    return () => {
      cancelled = true
      cleanup.current?.()
    }
  }, [settings, strings.preflight])

  const finish = (action: PreflightResult['action']) => {
    cleanup.current?.()
    onDone({ action })
  }

  const busy = server === 'pending' || mic === 'pending'
  const canRecord = server === 'pass' && mic === 'pass'

  return (
    <div className="bg-black/50 fixed inset-0 z-50 flex items-end justify-center p-4 sm:items-center">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={strings.preflight.title}
        className="bg-surface-1 border-line w-full max-w-sm rounded-2xl border p-5 shadow-2xl"
      >
        <h2 className="mb-4 text-lg font-semibold">{strings.preflight.title}</h2>

        <CheckRow state={server} label={serverMessage || strings.preflight.checking} />
        <CheckRow
          state={mic}
          label={micMessage || strings.preflight.checking}
          extra={<LevelMeter level={peak} />}
        />

        {server === 'fail' && <p className="text-fg-dim mt-3 text-sm">{strings.preflight.serverFailRemedy}</p>}
        {mic === 'fail' && micMessage === strings.preflight.micSilent && (
          <p className="text-fg-dim mt-3 text-sm">{strings.preflight.micSilentRemedy}</p>
        )}

        <div className="mt-5 flex flex-col gap-2">
          {canRecord && (
            <button
              type="button"
              onClick={() => finish('record')}
              aria-label={strings.preflight.startNow}
              className="bg-danger rounded-xl px-4 py-3 font-semibold text-white"
            >
              <span aria-hidden>● </span>
              {strings.record.start}
            </button>
          )}
          {server === 'fail' && (
            <button
              type="button"
              onClick={() => finish('offline')}
              className="bg-accent rounded-xl px-4 py-3 font-semibold text-white"
            >
              {strings.preflight.recordOffline}
            </button>
          )}
          {!canRecord && !busy && server !== 'fail' && (
            <button
              type="button"
              onClick={() => finish('record')}
              className="bg-surface-2 border-line rounded-xl border px-4 py-3"
            >
              {strings.preflight.recordAnyway}
            </button>
          )}
          <button type="button" onClick={() => finish('cancel')} className="text-fg-dim px-4 py-2 text-sm">
            {strings.preflight.cancel}
          </button>
        </div>
      </div>
    </div>
  )
}

function CheckRow({
  state,
  label,
  extra,
}: {
  state: CheckState
  label: string
  extra?: React.ReactNode
}) {
  const icon = state === 'pending' ? '…' : state === 'pass' ? '✓' : '✗'
  const colour = state === 'pending' ? 'text-fg-dim' : state === 'pass' ? 'text-good' : 'text-danger'
  return (
    <div className="flex items-center gap-3 py-1.5">
      <span className={`w-4 text-center font-bold ${colour}`} aria-hidden>
        {icon}
      </span>
      <span className="flex-1 text-sm">{label}</span>
      {extra}
    </div>
  )
}
