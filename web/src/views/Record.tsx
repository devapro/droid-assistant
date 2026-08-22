/**
 * Screen 1 — Record (SRS §5.1).
 *
 * The landing view, because it is the only time-critical action in the product:
 * a conversation does not wait. Everything here is arranged so that opening the
 * app leaves the user one tap from recording.
 */

import { useEffect, useState } from 'react'
import { api, type ModeInfo, type Preset } from '../api/client'
import { listInputDevices, connectionInfo, isSecureOrigin, BYTES_PER_HOUR_RAW, type DeviceInfo } from '../capture/recorder'
import { sourceSupport, type CaptureSource } from '../capture/sources'
import { Markdown } from '../components/Markdown'
import { Transcript } from '../components/Transcript'
import { CostBreakdown, formatCost } from '../components/CostBreakdown'
import { Button, LevelMeter, Pill, clock } from '../components/primitives'
import { t } from '../i18n'
import { useActionItems } from '../state/actionItems'
import { useRecording, type Recogniser } from '../state/recording'
import { PreflightDialog, type PreflightResult } from './Preflight'

export function Record({ onOpenSession }: { onOpenSession: (id: string) => void }) {
  const strings = t()
  const {
    state, elapsedMs, paused, offline, link, pendingChunks, droppedMs, level,
    utterances, partial, speakers, settings, notices, costUsd, costBreakdown,
    ceilingReached, sessionId, recogniser,
    start, stop, togglePause, mark, setMode, updateSettings, dismissNotice, reset,
  } = useRecording()

  const [devices, setDevices] = useState<DeviceInfo[]>([])
  const [modes, setModes] = useState<ModeInfo[]>([])
  const [presets, setPresets] = useState<Preset[]>([])
  const [languages, setLanguages] = useState<{ code: string; name: string }[]>([])
  const [multiLanguageWarning, setMultiLanguageWarning] = useState<string | null>(null)
  const [preflight, setPreflight] = useState(false)
  const [showSetup, setShowSetup] = useState(false)

  const recording = state === 'recording'
  const activeMode = modes.find((m) => m.mode === settings.mode)

  // A commitment is made *during* the conversation, and catching it as it goes
  // past is the whole point — waiting until the recording is over and hunting
  // for the line is the thing this feature exists to avoid.
  const actionItems = useActionItems(sessionId)
  const [showList, setShowList] = useState(false)
  // Nothing to switch to before anything is on the list, and the toggle goes
  // away with it rather than stranding the view on an empty panel.
  const hasList = Boolean(actionItems.list) && actionItems.count > 0
  const viewingList = showList && hasList

  useEffect(() => {
    void (async () => {
      const [modeInfo, langInfo, presetInfo] = await Promise.all([
        api.modes().catch(() => null),
        api.languages().catch(() => null),
        api.presets().catch(() => null),
      ])
      if (modeInfo) setModes(modeInfo.modes)
      if (langInfo) {
        setLanguages(langInfo.languages)
        setMultiLanguageWarning(langInfo.multi_language_warning)
      }
      if (presetInfo) setPresets(presetInfo.presets)
      setDevices(await listInputDevices().catch(() => []))
    })()
  }, [])

  // FR-CAP-12: warn before a long session on a connection that charges by the megabyte.
  useEffect(() => {
    const connection = connectionInfo()
    if (connection?.metered && !recording) {
      const mbPerHour = Math.round(BYTES_PER_HOUR_RAW / 1_000_000)
      useRecording.getState().notify({
        severity: 'warning',
        component: 'network',
        message: strings.errors.meteredConnection(mbPerHour),
      })
    }
  }, [recording, strings.errors])

  useEffect(() => {
    if (!isSecureOrigin()) {
      useRecording.getState().notify({
        severity: 'error',
        component: 'browser',
        message: strings.errors.insecureContext,
      })
    }
  }, [strings.errors.insecureContext])

  const beginRecording = async (result: PreflightResult) => {
    setPreflight(false)
    if (result.action === 'cancel') return
    await start(undefined, { offline: result.action === 'offline' })
  }

  return (
    <div className="flex h-full flex-col">
      {/* FR-UI-3: persistent and undismissable whenever capture is active. */}
      {recording && (
        <div
          role="status"
          aria-live="polite"
          className="bg-danger/15 border-danger/30 flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2"
        >
          <span className="bg-danger h-2.5 w-2.5 shrink-0 animate-pulse rounded-full" aria-hidden />
          <span className="text-danger text-sm font-semibold tracking-wide" data-testid="recording-indicator">
            {paused ? strings.record.paused : strings.record.recording}
          </span>
          <span className="font-mono text-sm tabular-nums">{clock(elapsedMs)}</span>
          {/* FR-UI-21: whether audio is leaving this machine, while it is
              happening. It belongs beside the recording indicator rather than in
              Settings, because it is a fact about the conversation in progress —
              and because Settings would answer from configuration, which this
              session may not be using. */}
          <RecogniserBadge recogniser={recogniser} />
          <span className="text-fg-dim ml-auto text-xs">
            {settings.languages.join('/').toUpperCase()} → {settings.targetLanguage.toUpperCase()}
            {' · '}
            {settings.mode}
          </span>
        </div>
      )}

      {/* Level and link state (FR-CAP-10, FR-UI-6). */}
      <div className="border-line flex items-center gap-3 border-b px-4 py-2">
        <LevelMeter level={level} />
        <div className="ml-auto flex items-center gap-2 text-xs">
          {recording && costUsd > 0 && (
            <details className="relative">
              <summary className="cursor-pointer list-none">
                <Pill tone={ceilingReached ? 'warn' : 'neutral'}>{formatCost(costUsd)}</Pill>
              </summary>
              <div className="bg-surface-1 border-line absolute right-0 z-20 mt-1 w-64 rounded-xl border p-3 shadow-xl">
                <CostBreakdown total={costUsd} breakdown={costBreakdown} />
              </div>
            </details>
          )}
          {droppedMs > 0 && <Pill tone="bad">{strings.connection.dropped(droppedMs)}</Pill>}
          <LinkBadge link={link} pending={pendingChunks} offline={offline} />
        </div>
      </div>

      <NoticeStack notices={notices} onDismiss={dismissNotice} />

      {/* A list built line by line during the conversation has to be readable
          during it too, or there is no way to see what you have collected
          without stopping the recording. */}
      {hasList && (
        <nav className="border-line flex gap-1 border-b px-3" role="tablist">
          {[
            { id: false, label: strings.record.transcriptTab },
            { id: true, label: strings.record.actionItemsTab(actionItems.count) },
          ].map((entry) => (
            <button
              key={String(entry.id)}
              type="button"
              role="tab"
              aria-selected={viewingList === entry.id}
              onClick={() => setShowList(entry.id)}
              className={`border-b-2 px-3 py-2 text-sm transition ${
                viewingList === entry.id ? 'border-accent text-fg' : 'text-fg-dim border-transparent'
              }`}
            >
              {entry.label}
            </button>
          ))}
        </nav>
      )}

      {viewingList ? (
        <div className="flex-1 overflow-y-auto px-4 py-3">
          <Markdown source={actionItems.list ?? ''} />
        </div>
      ) : /* FR-LAT-6: Batch mode shows no transcript while recording, and says so. */
      recording && settings.mode === 'batch' ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-4 px-8 text-center">
          <p className="font-mono text-5xl tabular-nums">{clock(elapsedMs)}</p>
          <LevelMeter level={level} className="scale-150" />
          <p className="text-fg-dim max-w-sm text-sm">{strings.record.batchNotice}</p>
        </div>
      ) : (
        <Transcript
          utterances={utterances}
          partial={partial}
          speakers={speakers}
          autoscroll
          // Only once there is a session to attach one to. The control is on
          // finalised lines only — a partial has no stored utterance to name.
          onActionItem={
            sessionId && actionItems.available
              ? (utterance) => void actionItems.extract(utterance)
              : undefined
          }
          actionItemBusyId={actionItems.busyId}
          actionItemSources={actionItems.sources}
          // The flag comes back on the event stream, so the line updates from
          // the same path the Mark button uses rather than from local state.
          onToggleMark={
            sessionId
              ? (utterance) =>
                  void api.editUtterance(utterance.utterance_id, { marked: !utterance.marked })
              : undefined
          }
          emptyTitle={recording ? strings.record.emptyTranscript : strings.record.idle}
          emptyAction={recording ? undefined : 'Press Record when you are ready.'}
        />
      )}

      {actionItems.notice && (
        <button
          type="button"
          onClick={actionItems.clearNotice}
          className="bg-accent/15 text-accent w-full px-4 py-2 text-left text-sm"
        >
          {actionItems.notice}
        </button>
      )}
      {actionItems.error && (
        <p className="bg-warn/15 text-warn px-4 py-2 text-sm">{actionItems.error}</p>
      )}

      {state === 'done' && sessionId && (
        <div className="border-line bg-surface-2 flex items-center gap-3 border-t px-4 py-3">
          <span className="text-sm">Session finished.</span>
          <button type="button" className="text-accent text-sm font-medium" onClick={() => onOpenSession(sessionId)}>
            Open it
          </button>
          <button type="button" className="text-fg-dim ml-auto text-sm" onClick={reset}>
            {strings.common.close}
          </button>
        </div>
      )}

      {/* Controls: within thumb reach at 375 px (FR-UI-5), two interactions from
          the main view to every capture choice (FR-UI-4). */}
      <div className="border-line bg-surface-1 border-t px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
        <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
          <select
            value={settings.languages[0] ?? 'en'}
            onChange={(event) => updateSettings({ languages: [event.target.value] })}
            disabled={recording}
            className="bg-surface-2 border-line rounded-lg border px-2 py-1.5 disabled:opacity-50"
            aria-label={strings.settings.languages}
          >
            {languages.map((language) => (
              <option key={language.code} value={language.code}>
                {language.name}
              </option>
            ))}
          </select>
          <span className="text-fg-dim">→</span>
          <select
            value={settings.targetLanguage}
            onChange={(event) => updateSettings({ targetLanguage: event.target.value })}
            disabled={recording}
            className="bg-surface-2 border-line rounded-lg border px-2 py-1.5 disabled:opacity-50"
            aria-label={strings.settings.targetLanguage}
          >
            {languages.map((language) => (
              <option key={language.code} value={language.code}>
                {language.name}
              </option>
            ))}
          </select>

          <select
            value={settings.mode}
            onChange={(event) => void setMode(event.target.value as 'live' | 'balanced' | 'batch')}
            // Capped, because a `select` is as wide as its longest *option* and
            // Live's option carries a sentence about what it costs. Unconstrained,
            // that one label made the control 429 px wide and the whole page
            // scroll sideways at 375 px (FR-UI-5). The closed control clips; the
            // open list does not, which is where the sentence is read anyway.
            className="bg-surface-2 border-line max-w-40 truncate rounded-lg border px-2 py-1.5"
            aria-label={strings.settings.mode}
          >
            {/* Every mode is offered. Live used to be greyed out against a
                recogniser that declared no streaming API, which was every local
                one — while the server had all along been running Live on those
                same recognisers by re-decoding a window. What is left to say
                about Live is what it costs, not whether it exists. */}
            {modes.map((mode) => (
              <option key={mode.mode} value={mode.mode}>
                {mode.mode}
                {mode.redecodes_window && !mode.uses_cloud_asr
                  ? ` — ${strings.settings.redecodesWindow}`
                  : ''}
              </option>
            ))}
          </select>

          <button
            type="button"
            onClick={() => setShowSetup((value) => !value)}
            className="text-fg-dim hover:text-fg ml-auto px-2 py-1.5"
            aria-label="More capture options"
          >
            ⚙
          </button>
        </div>

        {/* FR-LAT-7: what this mode costs and implies, where the choice is made. */}
        {activeMode && (
          <p className="text-fg-dim mb-3 text-xs">
            {activeMode.description}
            {activeMode.uses_cloud_asr && ' Audio leaves this server.'}
            {!activeMode.uses_cloud_asr && activeMode.uses_cloud_llm && ' Transcript text leaves this server for translation.'}
          </p>
        )}

        {multiLanguageWarning && settings.languages.length > 1 && (
          <p className="text-warn mb-3 text-xs">{multiLanguageWarning}</p>
        )}

        {showSetup && !recording && (
          <SetupPanel
            devices={devices}
            presets={presets}
            onPreset={(preset) => {
              updateSettings(preset.config as never)
              setShowSetup(false)
            }}
            onSavePreset={async (name) => {
              await api.savePreset(name, settings as unknown as Record<string, unknown>)
              setPresets((await api.presets()).presets)
            }}
          />
        )}

        <div className="flex items-center justify-center gap-3">
          {recording ? (
            <>
              <Button variant="ghost" onClick={() => void togglePause()} ariaLabel={paused ? strings.record.resume : strings.record.pause}>
                {paused ? '▶' : '❚❚'}
              </Button>
              <Button
                variant="stop"
                onClick={() => void stop()}
                className="min-w-40"
                ariaLabel={strings.record.stop}
              >
                <span aria-hidden>■ </span>
                {strings.record.stop}
              </Button>
              {/* FR-CAP-18: one interaction to flag the moment. */}
              <Button variant="ghost" onClick={mark} ariaLabel={strings.record.mark}>
                ⚑
              </Button>
            </>
          ) : (
            <Button
              variant="record"
              onClick={() => setPreflight(true)}
              disabled={state === 'permission' || state === 'stopping'}
              className="min-w-52"
              ariaLabel={strings.record.startAria}
            >
              {state === 'permission' ? (
                strings.record.preparing
              ) : state === 'stopping' ? (
                strings.record.stopping
              ) : (
                <>
                  <span aria-hidden>● </span>
                  {strings.record.start}
                </>
              )}
            </Button>
          )}
        </div>
      </div>

      {preflight && <PreflightDialog settings={settings} onDone={(result) => void beginRecording(result)} />}
    </div>
  )
}

/**
 * Which recogniser is running, and whether it is on this machine (FR-UI-21).
 *
 * Two questions, one badge, and the second is the one that matters: with a cloud
 * recogniser the room's audio is leaving the machine for as long as the recording
 * lasts. That was decided in Settings, possibly weeks ago and possibly by
 * per-language routing nobody remembers configuring, so it is stated here while
 * it is happening — coloured, because "cloud" is a consequence rather than a
 * detail. The full engine name is on the tooltip; the badge shows the service,
 * which is what the answer turns on.
 *
 * Nothing is rendered until the server has said. Guessing from configuration is
 * how a badge ends up claiming "local" over a session that is streaming audio to
 * a provider, and that mistake is worse than a moment with no badge.
 */
function RecogniserBadge({ recogniser }: { recogniser: Recogniser | null }) {
  const strings = t()
  if (!recogniser) return null
  const service = recogniser.name.split(':')[0]!.replace(/_/g, '-')
  return (
    <span
      data-testid="recogniser"
      title={
        recogniser.local
          ? strings.record.recogniserLocal(recogniser.name)
          : strings.record.recogniserCloud(recogniser.name)
      }
      className={`text-xs ${recogniser.local ? 'text-fg-dim' : 'text-warn font-medium'}`}
    >
      <span aria-hidden>{recogniser.local ? '⌂ ' : '☁ '}</span>
      {recogniser.local ? strings.record.onThisMachine : strings.record.inTheCloud} · {service}
    </span>
  )
}

function LinkBadge({ link, pending, offline }: { link: string; pending: number; offline: boolean }) {
  const strings = t()
  if (offline) return <Pill tone="warn">{strings.connection.offline}</Pill>
  switch (link) {
    case 'connected':
      return <Pill tone="good">● {strings.connection.connected}</Pill>
    case 'reconnecting':
      // Non-modal: capture continues, and the backlog is shown draining.
      return (
        <Pill tone="warn">
          ◐ {strings.connection.reconnecting}
          {pending > 0 && ` · ${strings.connection.buffered(pending)}`}
        </Pill>
      )
    case 'connecting':
      return <Pill tone="neutral">{strings.connection.connecting}</Pill>
    default:
      return null
  }
}

function NoticeStack({
  notices,
  onDismiss,
}: {
  notices: { id: string; severity: string; component: string; message: string; remedy?: string }[]
  onDismiss: (id: string) => void
}) {
  if (notices.length === 0) return null
  return (
    <div className="flex flex-col gap-1 px-3 py-2">
      {notices.map((notice) => (
        <div
          key={notice.id}
          role={notice.severity === 'error' ? 'alert' : 'status'}
          className={`flex items-start gap-2 rounded-lg px-3 py-2 text-sm ${
            notice.severity === 'error'
              ? 'bg-danger/15 text-danger'
              : notice.severity === 'warning'
                ? 'bg-warn/15 text-warn'
                : 'bg-surface-2 text-fg-dim'
          }`}
        >
          <div className="min-w-0 flex-1">
            {/* FR-UI-9: name the component, say what to do. */}
            <p>
              <span className="font-medium capitalize">{notice.component}: </span>
              {notice.message}
            </p>
            {notice.remedy && <p className="mt-0.5 text-xs opacity-80">{notice.remedy}</p>}
          </div>
          <button type="button" onClick={() => onDismiss(notice.id)} className="shrink-0 opacity-60" aria-label="Dismiss">
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}

function SetupPanel({
  devices,
  presets,
  onPreset,
  onSavePreset,
}: {
  devices: DeviceInfo[]
  presets: Preset[]
  onPreset: (preset: Preset) => void
  onSavePreset: (name: string) => Promise<void>
}) {
  const strings = t()
  const { settings, updateSettings } = useRecording()
  const [presetName, setPresetName] = useState('')
  const support = sourceSupport()

  return (
    <div className="border-line bg-surface-2 mb-3 flex flex-col gap-3 rounded-xl border p-3 text-sm">
      {/* FR-SES-14: a saved setup removes three decisions from every recording. */}
      {presets.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {presets.map((preset) => (
            <button
              key={preset.id}
              type="button"
              onClick={() => onPreset(preset)}
              className="bg-surface-3 hover:bg-surface-1 rounded-full px-3 py-1 text-xs"
            >
              {preset.name}
            </button>
          ))}
        </div>
      )}

      <label className="flex flex-col gap-1">
        <span className="text-fg-dim text-xs">{strings.settings.source}</span>
        <select
          value={settings.source}
          onChange={(event) => updateSettings({ source: event.target.value as CaptureSource })}
          className="bg-surface-1 border-line rounded-lg border px-2 py-1.5"
        >
          <option value="microphone">{strings.settings.sourceMicrophone}</option>
          <option value="system" disabled={!support.system}>
            {strings.settings.sourceSystem}
          </option>
          <option value="both" disabled={!support.system}>
            {strings.settings.sourceBoth}
          </option>
        </select>
        <span className="text-fg-dim text-xs">
          {support.system ? strings.settings.sourceHelp : strings.settings.sourceUnsupported}
        </span>
        {settings.source !== 'microphone' && (
          <span className="text-warn text-xs">{strings.settings.sourceConsent}</span>
        )}
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-fg-dim text-xs">{strings.settings.device}</span>
        <select
          value={settings.deviceId ?? ''}
          onChange={(event) => updateSettings({ deviceId: event.target.value || undefined })}
          className="bg-surface-1 border-line rounded-lg border px-2 py-1.5"
        >
          <option value="">System default</option>
          {devices.map((device) => (
            <option key={device.deviceId} value={device.deviceId}>
              {device.label}
            </option>
          ))}
        </select>
        <span className="text-fg-dim text-xs">{strings.settings.deviceHelp}</span>
      </label>

      <input
        value={settings.title ?? ''}
        onChange={(event) => updateSettings({ title: event.target.value })}
        placeholder="Title (optional)"
        className="bg-surface-1 border-line rounded-lg border px-2 py-1.5"
      />

      <div className="flex gap-2">
        <input
          value={presetName}
          onChange={(event) => setPresetName(event.target.value)}
          placeholder={strings.presets.namePlaceholder}
          className="bg-surface-1 border-line min-w-0 flex-1 rounded-lg border px-2 py-1.5"
        />
        <Button
          variant="default"
          disabled={!presetName.trim()}
          onClick={() => {
            void onSavePreset(presetName.trim())
            setPresetName('')
          }}
        >
          {strings.presets.save}
        </Button>
      </div>
    </div>
  )
}
