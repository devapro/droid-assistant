/**
 * Screen 4 — Settings (SRS §5.1).
 *
 * Grouped by what the operator is actually deciding, not by which config
 * section a value happens to live in. Anything that cannot take effect until
 * the next session says so, rather than appearing to apply immediately.
 *
 * Plugin forms are generated from each plugin's declared JSON schema (FR-PLG-5),
 * so a third-party plugin gets a correctly-typed settings form without shipping
 * any UI code.
 */

import { useEffect, useState } from 'react'
import {
  api,
  type Health,
  type JsonSchemaProperty,
  type ModelInfo,
  type ModelsResponse,
  type PluginInfo,
} from '../api/client'
import { listInputDevices, type DeviceInfo } from '../capture/recorder'
import { sourceSupport, type CaptureSource } from '../capture/sources'
import { Button, EmptyState, Pill } from '../components/primitives'
import { t } from '../i18n'
import { useRecording } from '../state/recording'

type Section = 'capture' | 'models' | 'backends' | 'plugins' | 'server'

export function Settings() {
  const strings = t()
  const [section, setSection] = useState<Section>('capture')
  const [health, setHealth] = useState<Health | null>(null)
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [trustNotice, setTrustNotice] = useState('')
  const [devices, setDevices] = useState<DeviceInfo[]>([])
  const [saving, setSaving] = useState(false)
  const [unreachable, setUnreachable] = useState(false)

  const refresh = async () => {
    // A failed health call used to leave every panel showing "Loading…"
    // indefinitely, which reads as a hang rather than as the server being
    // down. FR-UI-9: name the component and say what to do.
    try {
      setHealth(await api.health())
      setUnreachable(false)
    } catch {
      setUnreachable(true)
    }
    const pluginResult = await api.plugins().catch(() => null)
    if (pluginResult) {
      setPlugins(pluginResult.plugins)
      setTrustNotice(pluginResult.trust_notice)
    }
    setDevices(await listInputDevices().catch(() => []))
  }

  useEffect(() => {
    void refresh()
  }, [])

  const sections: [Section, string][] = [
    ['capture', strings.settings.capture],
    ['models', strings.settings.models],
    ['backends', strings.settings.backends],
    ['plugins', strings.settings.plugins],
    ['server', strings.settings.server],
  ]

  return (
    <div className="flex h-full flex-col">
      <header className="border-line border-b px-4 py-3">
        <h1 className="text-lg font-semibold">{strings.settings.title}</h1>
      </header>

      <nav className="border-line flex gap-1 overflow-x-auto border-b px-2" role="tablist">
        {sections.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={section === id}
            onClick={() => setSection(id)}
            className={`shrink-0 border-b-2 px-3 py-2 text-sm transition ${
              section === id ? 'border-accent text-fg' : 'text-fg-dim border-transparent'
            }`}
          >
            {label}
          </button>
        ))}
      </nav>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {unreachable && <ServerProblem onRetry={refresh} />}
        {health?.models?.state === 'loading' && <ModelsLoading detail={health.models.detail} />}
        {section === 'capture' && <CaptureSection devices={devices} />}
        {section === 'models' && <ModelsSection unreachable={unreachable} onChanged={refresh} />}
        {section === 'backends' && (
          <BackendsSection
            health={health}
            unreachable={unreachable}
            saving={saving}
            setSaving={setSaving}
            onSaved={refresh}
          />
        )}
        {section === 'plugins' && (
          <PluginsSection plugins={plugins} trustNotice={trustNotice} onChanged={refresh} />
        )}
        {section === 'server' && <ServerSection health={health} />}
      </div>
    </div>
  )
}

function CaptureSection({ devices }: { devices: DeviceInfo[] }) {
  const strings = t()
  const { settings, updateSettings } = useRecording()
  const support = sourceSupport()
  return (
    <div className="flex flex-col gap-5">
      <Field
        label={strings.settings.source}
        help={support.system ? strings.settings.sourceHelp : strings.settings.sourceUnsupported}
      >
        <select
          value={settings.source}
          onChange={(event) => updateSettings({ source: event.target.value as CaptureSource })}
          className="bg-surface-2 border-line w-full rounded-lg border px-3 py-2"
        >
          <option value="microphone">{strings.settings.sourceMicrophone}</option>
          <option value="system" disabled={!support.system}>
            {strings.settings.sourceSystem}
          </option>
          <option value="both" disabled={!support.system}>
            {strings.settings.sourceBoth}
          </option>
        </select>
        {settings.source !== 'microphone' && (
          <p className="text-warn mt-1 text-xs">{strings.settings.sourceConsent}</p>
        )}
      </Field>

      <Field label={strings.settings.device} help={strings.settings.deviceHelp}>
        <select
          value={settings.deviceId ?? ''}
          onChange={(event) => updateSettings({ deviceId: event.target.value || undefined })}
          className="bg-surface-2 border-line w-full rounded-lg border px-3 py-2"
        >
          <option value="">System default</option>
          {devices.map((device) => (
            <option key={device.deviceId} value={device.deviceId}>
              {device.label}
            </option>
          ))}
        </select>
      </Field>

      <Field label={strings.settings.audioProcessing} help={strings.settings.audioProcessingHelp}>
        <div className="flex flex-col gap-2">
          <Toggle
            label={strings.settings.echoCancellation}
            checked={settings.echoCancellation}
            onChange={(value) => updateSettings({ echoCancellation: value })}
          />
          <Toggle
            label={strings.settings.noiseSuppression}
            checked={settings.noiseSuppression}
            onChange={(value) => updateSettings({ noiseSuppression: value })}
          />
          <Toggle
            label={strings.settings.autoGainControl}
            checked={settings.autoGainControl}
            onChange={(value) => updateSettings({ autoGainControl: value })}
          />
        </div>
      </Field>

      <Field label="Custom vocabulary" help="Names and terms the recogniser should expect. One per line.">
        <textarea
          value={settings.vocabulary.join('\n')}
          onChange={(event) =>
            updateSettings({ vocabulary: event.target.value.split('\n').map((s) => s.trim()).filter(Boolean) })
          }
          rows={4}
          className="bg-surface-2 border-line w-full rounded-lg border px-3 py-2 font-mono text-sm"
        />
      </Field>

      <Field label={strings.settings.theme}>
        <ThemePicker />
      </Field>
    </div>
  )
}

function ServerProblem({ onRetry }: { onRetry: () => Promise<void> }) {
  const strings = t()
  return (
    <div role="alert" className="bg-danger/15 text-danger mb-4 rounded-xl p-3 text-sm">
      <p className="font-medium">{strings.settings.unreachable}</p>
      <p className="mt-0.5 text-xs opacity-90">{strings.settings.unreachableRemedy}</p>
      <button
        type="button"
        onClick={() => void onRetry()}
        className="border-danger/40 mt-2 rounded-lg border px-3 py-1.5 text-xs"
      >
        {strings.common.retry}
      </button>
    </div>
  )
}

function ModelsLoading({ detail }: { detail: string }) {
  const strings = t()
  return (
    <div role="status" className="bg-warn/15 text-warn mb-4 rounded-xl p-3 text-sm">
      <p className="font-medium">{strings.settings.modelsLoading}</p>
      <p className="mt-0.5 text-xs opacity-90">{detail || strings.settings.modelsLoadingRemedy}</p>
    </div>
  )
}

/**
 * Speech models (FR-ASR-1, FR-ASR-7).
 *
 * The decision this screen exists for is not "which model is best" — it is
 * "which model for which language", because the answer genuinely differs and
 * the operator had no way to express it without editing config.toml and
 * restarting. A model has to be on this server before it can be selected, so
 * the picker offers what is present and the rest is a download away.
 */
function ModelsSection({
  unreachable,
  onChanged,
}: {
  unreachable: boolean
  onChanged: () => Promise<void>
}) {
  const strings = t()
  const [data, setData] = useState<ModelsResponse | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  const load = async () => {
    const result = await api.models().catch(() => null)
    if (result) setData(result)
  }

  useEffect(() => {
    void load()
  }, [])

  // A download runs for minutes on the server with no event of its own, so the
  // only honest way to show it finishing is to keep asking. Polling stops the
  // moment nothing is in flight.
  const downloading = data?.models.some((m) => m.state === 'downloading') ?? false
  useEffect(() => {
    if (!downloading) return
    const timer = setInterval(() => void load(), 3000)
    return () => clearInterval(timer)
  }, [downloading])

  if (!data) {
    return unreachable ? null : <p className="text-fg-dim text-sm">{strings.common.loading}</p>
  }

  const apply = async (body: Record<string, unknown>) => {
    setBusy(true)
    try {
      const result = await api.patchConfig(body)
      setMessage(result.applies_to)
      await load()
      await onChanged()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : strings.errors.generic)
    } finally {
      setBusy(false)
    }
  }

  const startDownload = async (id: string) => {
    // Mark it locally so the button changes on the first click rather than on
    // the next poll — three seconds of an unchanged button reads as ignored.
    setData({
      ...data,
      models: data.models.map((m) => (m.id === id ? { ...m, state: 'downloading' } : m)),
    })
    await api.downloadModel(id).catch(() => null)
    await load()
  }

  const usable = (model: ModelInfo) => model.state === 'present' || model.state === 'ready'

  return (
    <div className="flex flex-col gap-6">
      <Field label={strings.settings.defaultModel} help={strings.settings.defaultModelHelp}>
        <ModelSelect
          models={data.models.filter((m) => usable(m) || m.id === data.default)}
          value={data.default}
          disabled={busy}
          onChange={(id) => {
            const model = data.models.find((m) => m.id === id)
            if (model) void apply({ asr_backend: model.backend, asr_model: model.model })
          }}
        />
      </Field>

      <Field label={strings.settings.perLanguage} help={strings.settings.perLanguageHelp}>
        <div className="flex flex-col gap-2">
          {data.languages.map((code) => {
            const route = data.routing[code]
            const choices = data.models.filter(
              // A model that cannot recognise this language must not be
              // offered for it: handed the wrong language it does not fail,
              // it returns fluent nonsense.
              (m) => (usable(m) || m.id === route?.id) && (m.languages === null || m.languages.includes(code)),
            )
            return (
              <div key={code} className="flex items-center gap-3">
                <span className="w-28 shrink-0 text-sm">{languageName(code)}</span>
                <ModelSelect
                  models={choices}
                  value={route?.explicit ? route.id : ''}
                  disabled={busy}
                  // Not "use default — <name>": the default is named in full
                  // three rows above, and repeating it here only truncated.
                  placeholder={strings.settings.useDefault}
                  onChange={(id) => void apply({ asr_by_language: { [code]: id } })}
                />
              </div>
            )
          })}
        </div>
      </Field>

      {message && <p className="text-fg-dim text-sm">{message}</p>}

      <Field label={strings.settings.availableModels} help={strings.settings.availableModelsHelp}>
        <div className="flex flex-col gap-2">
          {data.models.map((model) => (
            <ModelRow key={model.id} model={model} onDownload={() => void startDownload(model.id)} />
          ))}
        </div>
      </Field>

      <p className="text-fg-dim font-mono text-xs">
        {strings.settings.modelsDir}: {data.models_dir}
      </p>
    </div>
  )
}

function ModelSelect({
  models,
  value,
  onChange,
  disabled,
  placeholder,
}: {
  models: ModelInfo[]
  value: string
  onChange: (id: string) => void
  disabled?: boolean
  placeholder?: string
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      className="bg-surface-2 border-line w-full rounded-lg border px-3 py-2 disabled:opacity-50"
    >
      {placeholder !== undefined && <option value="">{placeholder}</option>}
      {/* The name only. A native select cannot wrap, so appending each model's
          note truncated every option mid-sentence — and the notes are already
          right below, in full, on the same screen. */}
      {models.map((model) => (
        <option key={model.id} value={model.id}>
          {label(model.id)}
        </option>
      ))}
    </select>
  )
}

function ModelRow({ model, onDownload }: { model: ModelInfo; onDownload: () => void }) {
  const strings = t()
  const tone = { present: 'good', ready: 'good', downloading: 'warn', failed: 'bad' } as const
  const stateLabel = {
    present: `${strings.settings.onDisk} · ${model.size_mb} MB`,
    ready: strings.settings.cloudReady,
    absent: strings.settings.notDownloaded,
    downloading: strings.settings.downloading,
    failed: 'failed',
    unavailable: strings.settings.noCredential,
  }[model.state]

  return (
    <div className="border-line bg-surface-2 flex items-center gap-3 rounded-xl border p-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-sm">{label(model.id)}</span>
          <Pill tone={tone[model.state as keyof typeof tone] ?? 'neutral'}>{stateLabel}</Pill>
          {model.languages !== null && model.languages.length <= 4 && (
            <span className="text-fg-dim text-xs">
              {strings.settings.onlyCovers} {model.languages.join(', ')}
            </span>
          )}
        </div>
        {model.note && <p className="text-fg-dim mt-0.5 text-xs">{model.note}</p>}
        {model.error && <p className="text-danger mt-0.5 text-xs">{model.error}</p>}
      </div>
      {model.local && (model.state === 'absent' || model.state === 'failed') && (
        <Button onClick={onDownload}>
          {strings.settings.download}
          <span className="ml-1 opacity-70">~{model.size_mb} MB</span>
        </Button>
      )}
    </div>
  )
}

/** `faster_whisper:large-v3-turbo` → `faster-whisper · large-v3-turbo`. */
function label(id: string): string {
  const [backend, ...rest] = id.split(':')
  return `${backend!.replace(/_/g, '-')} · ${rest.join(':')}`
}

const languageNames =
  typeof Intl !== 'undefined' && 'DisplayNames' in Intl
    ? new Intl.DisplayNames(['en'], { type: 'language' })
    : null

function languageName(code: string): string {
  try {
    return languageNames?.of(code) ?? code
  } catch {
    return code
  }
}

function BackendsSection({
  health,
  unreachable,
  saving,
  setSaving,
  onSaved,
}: {
  health: Health | null
  unreachable: boolean
  saving: boolean
  setSaving: (value: boolean) => void
  onSaved: () => Promise<void>
}) {
  const strings = t()
  const [localOnly, setLocalOnly] = useState(health?.local_only ?? false)
  const [ceiling, setCeiling] = useState('0')
  const [message, setMessage] = useState<string | null>(null)

  useEffect(() => setLocalOnly(health?.local_only ?? false), [health?.local_only])

  // The unreachable banner is rendered once, above every section — repeating
  // it here said the same thing twice. With nothing to show, this section says
  // nothing rather than spinning forever.
  if (!health) {
    return unreachable ? null : <p className="text-fg-dim text-sm">{strings.common.loading}</p>
  }

  const save = async (body: Record<string, unknown>) => {
    setSaving(true)
    try {
      const result = await api.patchConfig(body)
      setMessage(result.applies_to)
      await onSaved()
    } catch (error) {
      setMessage(error instanceof Error ? error.message : strings.errors.generic)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <BackendCard
        title="Speech recognition"
        name={health.backends.asr.name}
        local={health.backends.asr.local}
        extra={health.backends.asr.streaming ? 'streaming' : 'batch only — Live mode unavailable'}
      />
      <BackendCard
        title="Diarization"
        name={String(health.backends.diarization.name ?? 'disabled')}
        local={Boolean(health.backends.diarization.local)}
      />
      <BackendCard
        title="Translation"
        name={String(health.backends.translation.name ?? 'disabled')}
        local={Boolean(health.backends.translation.local)}
        available={health.backends.translation.available !== false}
        extra={health.backends.translation.uses_context ? 'uses conversational context' : 'sentence by sentence'}
      />
      <BackendCard
        title="Language model"
        name={health.backends.llm.model || 'not configured'}
        local={health.backends.llm.local}
        available={health.backends.llm.available}
      />

      <Field label={strings.settings.localOnly} help={strings.settings.localOnlyHelp}>
        <Toggle
          label={strings.settings.localOnly}
          checked={localOnly}
          onChange={(value) => {
            setLocalOnly(value)
            void save({ local_only: value })
          }}
        />
      </Field>

      <Field label={strings.settings.costCeiling} help={strings.settings.costCeilingHelp}>
        <div className="flex gap-2">
          <input
            type="number"
            min={0}
            step={0.25}
            value={ceiling}
            onChange={(event) => setCeiling(event.target.value)}
            className="bg-surface-2 border-line w-32 rounded-lg border px-3 py-2"
          />
          <Button onClick={() => void save({ session_cost_ceiling_usd: Number(ceiling) })} disabled={saving}>
            {strings.common.save}
          </Button>
        </div>
      </Field>

      {message && <p className="text-fg-dim text-sm">{message}</p>}
      {health.warnings.length > 0 && (
        <div className="bg-warn/10 text-warn rounded-xl p-3 text-sm">
          {health.warnings.map((warning, index) => (
            <p key={index} className="mb-1 last:mb-0">
              {warning}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

function BackendCard({
  title,
  name,
  local,
  available = true,
  extra,
}: {
  title: string
  name: string
  local: boolean
  available?: boolean
  extra?: string
}) {
  return (
    <div className="border-line bg-surface-2 rounded-xl border p-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">{title}</span>
        {/* "local" would be a misleading badge for a backend that is not
            configured at all — where data goes is exactly what this card is for. */}
        {available ? (
          <Pill tone={local ? 'good' : 'warn'}>{local ? 'local' : 'cloud'}</Pill>
        ) : (
          <Pill tone="neutral">not configured</Pill>
        )}
      </div>
      <p className="text-fg-dim mt-1 font-mono text-sm">{name}</p>
      {extra && <p className="text-fg-dim mt-0.5 text-xs">{extra}</p>}
    </div>
  )
}

function PluginsSection({
  plugins,
  trustNotice,
  onChanged,
}: {
  plugins: PluginInfo[]
  trustNotice: string
  onChanged: () => Promise<void>
}) {
  const strings = t()
  if (plugins.length === 0) {
    return <EmptyState title={strings.settings.noPlugins} action={strings.settings.noPluginsAction} />
  }
  return (
    <div className="flex flex-col gap-4">
      {/* NFR-SEC-6 / R11: the trust boundary, stated where plugins are managed. */}
      <p className="bg-warn/10 text-warn rounded-xl p-3 text-sm">⚠ {trustNotice}</p>
      {plugins.map((plugin) => (
        <PluginCard key={plugin.name} plugin={plugin} onChanged={onChanged} />
      ))}
    </div>
  )
}

function PluginCard({ plugin, onChanged }: { plugin: PluginInfo; onChanged: () => Promise<void> }) {
  const [config, setConfig] = useState(plugin.config)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const strings = t()

  const properties = Object.entries(plugin.config_schema.properties ?? {})

  const save = async () => {
    try {
      await api.patchPlugin(plugin.name, { config })
      setDirty(false)
      setError(null)
      await onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : strings.errors.generic)
    }
  }

  return (
    <div className="border-line bg-surface-2 rounded-xl border p-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-medium">{plugin.name}</span>
            <span className="text-fg-dim text-xs">v{plugin.version}</span>
            {!plugin.available && <Pill tone="warn">unavailable</Pill>}
          </div>
          <p className="text-fg-dim text-sm">{plugin.description}</p>
          {plugin.unavailable_reason && plugin.enabled && (
            <p className="text-warn mt-1 text-xs">{plugin.unavailable_reason}</p>
          )}
          {plugin.last_error && <p className="text-danger mt-1 text-xs">Last error: {plugin.last_error}</p>}
        </div>
        <Toggle
          label=""
          checked={plugin.enabled}
          onChange={(value) => {
            void api.patchPlugin(plugin.name, { enabled: value }).then(onChanged)
          }}
        />
      </div>

      {/* FR-PLG-5: a form rendered from the plugin's own declared schema. */}
      {plugin.enabled && properties.length > 0 && (
        <div className="border-line mt-3 flex flex-col gap-2 border-t pt-3">
          {properties.map(([key, schema]) => (
            <SchemaField
              key={key}
              name={key}
              schema={schema}
              value={config[key]}
              onChange={(value) => {
                setConfig({ ...config, [key]: value })
                setDirty(true)
              }}
            />
          ))}
          {dirty && (
            <div className="flex items-center gap-2">
              <Button onClick={() => void save()}>{strings.common.save}</Button>
              {error && <span className="text-danger text-xs">{error}</span>}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function SchemaField({
  name,
  schema,
  value,
  onChange,
}: {
  name: string
  schema: JsonSchemaProperty
  value: unknown
  onChange: (value: unknown) => void
}) {
  const label = schema.title ?? name.replace(/_/g, ' ')

  if (schema.enum) {
    return (
      <label className="flex flex-col gap-1 text-sm">
        <span className="capitalize">{label}</span>
        <select
          value={String(value ?? schema.default ?? '')}
          onChange={(event) => onChange(event.target.value)}
          className="bg-surface-1 border-line rounded-lg border px-2 py-1.5"
        >
          {schema.enum.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        {schema.description && <span className="text-fg-dim text-xs">{schema.description}</span>}
      </label>
    )
  }

  if (schema.type === 'boolean') {
    return (
      <Toggle
        label={String(label)}
        checked={Boolean(value ?? schema.default)}
        onChange={onChange}
        help={schema.description}
      />
    )
  }

  if (schema.type === 'integer' || schema.type === 'number') {
    return (
      <label className="flex flex-col gap-1 text-sm">
        <span className="capitalize">{label}</span>
        <input
          type="number"
          min={schema.minimum}
          max={schema.maximum}
          value={Number(value ?? schema.default ?? 0)}
          onChange={(event) => onChange(Number(event.target.value))}
          className="bg-surface-1 border-line w-32 rounded-lg border px-2 py-1.5"
        />
        {schema.description && <span className="text-fg-dim text-xs">{schema.description}</span>}
      </label>
    )
  }

  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="capitalize">{label}</span>
      <input
        value={String(value ?? schema.default ?? '')}
        onChange={(event) => onChange(event.target.value)}
        className="bg-surface-1 border-line rounded-lg border px-2 py-1.5"
      />
      {schema.description && <span className="text-fg-dim text-xs">{schema.description}</span>}
    </label>
  )
}

function ServerSection({ health }: { health: Health | null }) {
  const strings = t()
  // The unreachable banner is rendered above this, so a bare loading line here
  // only ever means a request genuinely in flight.
  if (!health) return <p className="text-fg-dim text-sm">{strings.common.loading}</p>
  const disk = health.disk
  return (
    <div className="flex flex-col gap-4 text-sm">
      <Row label="Version" value={health.version} />
      <Row label="Status" value={health.status} tone={health.status === 'ok' ? 'good' : 'warn'} />
      <Row
        label="Speech model"
        value={`${health.models.backend} · ${health.models.state}`}
        tone={
          health.models.state === 'ready'
            ? 'good'
            : health.models.state === 'failed'
              ? 'bad'
              : 'warn'
        }
      />
      <Row label="Uptime" value={`${Math.round(health.uptime_s / 60)} min`} />
      <Row label="GPU" value={health.gpu.present ? `${health.gpu.cuda_devices} CUDA device(s)` : 'none (CPU)'} />
      {disk.available && (
        <>
          <Row
            label="Free disk"
            value={`${Math.round((disk.free_mb ?? 0) / 1024)} GB of ${Math.round((disk.total_mb ?? 0) / 1024)} GB`}
            tone={disk.below_minimum ? 'bad' : disk.low ? 'warn' : 'good'}
          />
          {disk.low && (
            <p className="text-warn text-xs">
              Recording is refused below the configured minimum, so a session can never fail part-way
              through by running out of space.
            </p>
          )}
        </>
      )}
      {health.errors.length > 0 && (
        <div className="bg-danger/10 text-danger rounded-xl p-3">
          {health.errors.map((error, index) => (
            <p key={index}>{error}</p>
          ))}
        </div>
      )}
    </div>
  )
}

function Row({ label, value, tone }: { label: string; value: string; tone?: 'good' | 'warn' | 'bad' }) {
  return (
    <div className="border-line flex items-center justify-between border-b pb-2">
      <span className="text-fg-dim">{label}</span>
      {tone ? <Pill tone={tone}>{value}</Pill> : <span className="font-mono">{value}</span>}
    </div>
  )
}

function Field({ label, help, children }: { label: string; help?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-sm font-medium">{label}</span>
      {children}
      {help && <span className="text-fg-dim text-xs">{help}</span>}
    </div>
  )
}

function Toggle({
  label,
  checked,
  onChange,
  help,
}: {
  label: string
  checked: boolean
  onChange: (value: boolean) => void
  help?: string
}) {
  return (
    <label className="flex cursor-pointer items-center gap-3 text-sm">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label || 'Toggle'}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-10 shrink-0 rounded-full transition ${checked ? 'bg-accent' : 'bg-surface-3'}`}
      >
        <span
          className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all ${
            checked ? 'left-[1.125rem]' : 'left-0.5'
          }`}
        />
      </button>
      <span className="flex flex-col">
        <span>{label}</span>
        {help && <span className="text-fg-dim text-xs">{help}</span>}
      </span>
    </label>
  )
}

/** FR-UI-11: both themes designed, following the device with a manual override. */
function ThemePicker() {
  const strings = t()
  const [theme, setTheme] = useState(() => localStorage.getItem('droid.theme') ?? 'system')
  const apply = (value: string) => {
    setTheme(value)
    localStorage.setItem('droid.theme', value)
    document.documentElement.dataset.theme = value === 'system' ? '' : value
  }
  return (
    <div className="flex gap-2">
      {[
        ['system', strings.settings.themeSystem],
        ['light', strings.settings.themeLight],
        ['dark', strings.settings.themeDark],
      ].map(([value, label]) => (
        <button
          key={value}
          type="button"
          onClick={() => apply(value!)}
          className={`rounded-lg px-3 py-1.5 text-sm ${
            theme === value ? 'bg-accent text-white' : 'bg-surface-2 border-line border'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  )
}
