/**
 * Microphone capture (FR-CAP-1 … FR-CAP-11).
 *
 * `getUserMedia` → `AudioWorklet` → 16 kHz mono → 200 ms int16 chunks, plus the
 * live level meter and detection of capture stopping unexpectedly.
 *
 * Browser audio processing (echo cancellation, noise suppression, AGC) is
 * exposed rather than assumed. All three are tuned for a single near voice on a
 * call, and noise suppression in particular will attenuate the quieter people
 * at a meeting table — the exact recording this product is for. They default
 * off, and the values used are recorded in session metadata so an accuracy
 * measurement can be attributed to them (FR-CAP-11, R6).
 */

import { captureSystemAudio, describeStream, type CaptureSource } from './sources'

export interface AudioProcessingOptions {
  echoCancellation: boolean
  noiseSuppression: boolean
  autoGainControl: boolean
}

export interface RecorderOptions extends AudioProcessingOptions {
  deviceId?: string
  /** Microphone, this machine's playback, or both mixed (SRS §2.1). */
  source?: CaptureSource
  chunkMs: number
  onChunk: (pcm: ArrayBuffer, tMs: number) => void
  onLevel: (peak: number) => void
  /** Capture stopped without us asking — unplugged device, revoked permission. */
  onEnded: (reason: string) => void
}

export interface DeviceInfo {
  deviceId: string
  label: string
  isDefault: boolean
}

export class MicrophonePermissionError extends Error {
  constructor(
    message: string,
    readonly remedy: string,
  ) {
    super(message)
    this.name = 'MicrophonePermissionError'
  }
}

export class Recorder {
  private context: AudioContext | null = null
  private node: AudioWorkletNode | null = null
  private inputs: MediaStreamAudioSourceNode[] = []
  private gains: GainNode[] = []
  /** The microphone stream, when one is in use. */
  private stream: MediaStream | null = null
  /** The display-capture stream, when one is in use. */
  private display: MediaStream | null = null
  private elapsedMs = 0
  private stopping = false

  constructor(private readonly options: RecorderOptions) {}

  get active(): boolean {
    return this.node !== null
  }

  get sampleRateIn(): number {
    return this.context?.sampleRate ?? 0
  }

  get trackLabel(): string {
    return [describeStream(this.stream), describeStream(this.display)]
      .filter(Boolean)
      .join(' + ')
  }

  private get source(): CaptureSource {
    return this.options.source ?? 'microphone'
  }

  /** Recorded in session metadata, so an accuracy figure can be attributed to
   *  the signal chain that produced it (FR-CAP-11, R6). */
  get constraintsApplied(): AudioProcessingOptions & {
    sampleRateIn: number
    source: CaptureSource
    trackLabel: string
  } {
    const settings = this.stream?.getAudioTracks()[0]?.getSettings() ?? {}
    return {
      echoCancellation: Boolean(settings.echoCancellation),
      noiseSuppression: Boolean(settings.noiseSuppression),
      autoGainControl: Boolean(settings.autoGainControl),
      sampleRateIn: this.sampleRateIn,
      source: this.source,
      trackLabel: this.trackLabel,
    }
  }

  async start(): Promise<void> {
    if (this.node) return
    this.stopping = false

    // Display capture first: it opens a picker, and failing after the
    // microphone is already live would leave a permission granted for nothing.
    if (this.source !== 'microphone') {
      this.display = await captureSystemAudio()
    }
    if (this.source !== 'system') {
      const constraints: MediaStreamConstraints = {
        audio: {
          deviceId: this.options.deviceId ? { exact: this.options.deviceId } : undefined,
          channelCount: { ideal: 1 },
          echoCancellation: this.options.echoCancellation,
          noiseSuppression: this.options.noiseSuppression,
          autoGainControl: this.options.autoGainControl,
        },
        video: false,
      }
      try {
        this.stream = await navigator.mediaDevices.getUserMedia(constraints)
      } catch (error) {
        this.display?.getTracks().forEach((track) => track.stop())
        this.display = null
        throw translateGetUserMediaError(error)
      }
    }

    // Do not pin the context to 16 kHz: forcing a rate the hardware does not
    // support makes some browsers resample badly or refuse outright. Resampling
    // happens in the worklet, where we control the quality.
    this.context = new AudioContext({ latencyHint: 'interactive' })
    if (this.context.state === 'suspended') await this.context.resume()

    await this.context.audioWorklet.addModule('/capture-worklet.js')

    this.node = new AudioWorkletNode(this.context, 'capture-processor', {
      numberOfInputs: 1,
      numberOfOutputs: 0,
      processorOptions: { chunkMs: this.options.chunkMs },
    })

    // Web Audio sums everything connected to one input, so mixing is just two
    // connections. Headroom matters though: a microphone and a video call at
    // full scale clip when added, and clipping is not recoverable downstream.
    const active = [this.stream, this.display].filter(Boolean) as MediaStream[]
    const gain = active.length > 1 ? 0.7 : 1
    for (const stream of active) {
      const input = this.context.createMediaStreamSource(stream)
      const level = this.context.createGain()
      level.gain.value = gain
      input.connect(level).connect(this.node)
      this.inputs.push(input)
      this.gains.push(level)
    }

    this.node.port.onmessage = (event: MessageEvent) => {
      const data = event.data
      if (data?.type !== 'chunk') return
      this.options.onChunk(data.pcm as ArrayBuffer, this.elapsedMs)
      this.elapsedMs += this.options.chunkMs
      this.options.onLevel(data.peak as number)
    }

    // FR-CAP-9: a track ending on its own is a real failure the user must see —
    // a revoked permission, a microphone unplugged mid-meeting, or the browser's
    // own "Stop sharing" button, which is very easy to hit by accident.
    for (const track of this.stream?.getAudioTracks() ?? []) {
      track.addEventListener('ended', () => {
        if (!this.stopping) {
          this.options.onEnded(
            'the microphone stopped — it may have been unplugged or its permission revoked',
          )
        }
      })
      track.addEventListener('mute', () => {
        if (!this.stopping) this.options.onLevel(0)
      })
    }
    for (const track of this.display?.getTracks() ?? []) {
      track.addEventListener('ended', () => {
        if (this.stopping) return
        if (this.source === 'both') {
          // The microphone is still live, so the recording continues rather
          // than ending — but half the audio has just gone, and saying nothing
          // would be the worst outcome.
          this.options.onEnded(
            'screen sharing stopped, so audio from this machine is no longer being recorded',
          )
        } else {
          this.options.onEnded('screen sharing stopped, so recording has ended')
        }
      })
    }
  }

  async stop(): Promise<void> {
    if (this.stopping) return
    this.stopping = true
    this.node?.port.postMessage({ type: 'stop' })
    this.node?.disconnect()
    this.inputs.forEach((input) => input.disconnect())
    this.gains.forEach((level) => level.disconnect())
    this.stream?.getTracks().forEach((track) => track.stop())
    // Stopping the video track is what actually ends the browser's "sharing"
    // banner, so it matters that this runs.
    this.display?.getTracks().forEach((track) => track.stop())
    if (this.context && this.context.state !== 'closed') {
      await this.context.close().catch(() => undefined)
    }
    this.node = null
    this.inputs = []
    this.gains = []
    this.stream = null
    this.display = null
    this.context = null
  }
}

function translateGetUserMediaError(error: unknown): MicrophonePermissionError {
  const name = (error as { name?: string })?.name ?? 'Error'
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return new MicrophonePermissionError(
        'Microphone permission was denied.',
        'Enable the microphone for this site in your browser settings, then press Record again.',
      )
    case 'NotFoundError':
    case 'OverconstrainedError':
      return new MicrophonePermissionError(
        'No microphone matching that selection was found.',
        'Choose a different input device in Settings, or plug one in.',
      )
    case 'NotReadableError':
      return new MicrophonePermissionError(
        'The microphone is in use by another application.',
        'Close the other app using it — a call or a meeting client — and try again.',
      )
    default:
      return new MicrophonePermissionError(
        `Could not start capture (${name}).`,
        isSecureContext
          ? 'Reload the page and try again.'
          : 'This page is not on a secure origin. Browsers only allow microphone access over HTTPS or on localhost — see the HTTPS section of the README.',
      )
  }
}

/** FR-CAP-2. Labels are empty until permission has been granted at least once. */
export async function listInputDevices(): Promise<DeviceInfo[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return []
  const devices = await navigator.mediaDevices.enumerateDevices()
  return devices
    .filter((device) => device.kind === 'audioinput')
    .map((device, index) => ({
      deviceId: device.deviceId,
      label: device.label || `Microphone ${index + 1}`,
      isDefault: device.deviceId === 'default' || device.deviceId === '',
    }))
}

export function isSecureOrigin(): boolean {
  return window.isSecureContext
}

/** FR-CAP-12. `connection` is Chromium-only, so absence means "cannot tell". */
export function connectionInfo(): { metered: boolean; type: string } | null {
  const connection = (navigator as { connection?: { saveData?: boolean; effectiveType?: string; type?: string } })
    .connection
  if (!connection) return null
  const type = connection.type ?? connection.effectiveType ?? 'unknown'
  return {
    metered: Boolean(connection.saveData) || type === 'cellular' || type === '2g' || type === '3g',
    type,
  }
}

/** Raw PCM at 16 kHz mono int16 — the figure the metered-connection warning quotes. */
export const BYTES_PER_HOUR_RAW = 16000 * 2 * 3600
