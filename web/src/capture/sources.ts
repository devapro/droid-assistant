/**
 * Where the audio comes from: the microphone, this machine's playback, or both.
 *
 * The microphone hears a video call through the room's speakers; display
 * capture takes the same audio *before* it becomes sound in a room. That skips
 * the loudspeaker, the room, and the microphone — three lossy stages — so a
 * remote participant transcribes about as well as someone sitting next to you.
 * It is the single largest accuracy gain available for online meetings, and it
 * needs no per-platform integration (SRS §2.1).
 *
 * Three things about `getDisplayMedia` shape this module:
 *
 * 1. **You cannot ask for audio alone.** The spec requires a video track, so we
 *    request one, never render it, and disable it immediately. Stopping it
 *    outright ends the whole capture session in Chrome, so it stays alive and
 *    idle.
 * 2. **Audio is opt-in inside the picker.** A user who shares a tab without
 *    ticking "share tab audio" gets a stream with no audio track at all. That
 *    is the commonest failure and needs naming, not a stack trace.
 * 3. **Support is uneven.** Chrome and Edge do tab audio everywhere and system
 *    audio on Windows and ChromeOS. macOS offers no system audio at all, only
 *    tab audio. Firefox and Safari offer neither.
 */

export type CaptureSource = 'microphone' | 'system' | 'both'

export interface SourceSupport {
  microphone: boolean
  system: boolean
  /** Why display capture is unavailable, when it is. */
  reason?: string
}

export class DisplayCaptureError extends Error {
  constructor(
    message: string,
    readonly remedy: string,
  ) {
    super(message)
    this.name = 'DisplayCaptureError'
  }
}

export function sourceSupport(): SourceSupport {
  const microphone = Boolean(navigator.mediaDevices?.getUserMedia)
  if (!navigator.mediaDevices?.getDisplayMedia) {
    return {
      microphone,
      system: false,
      reason:
        'This browser cannot capture audio playing on this machine. Chrome or Edge on a desktop can.',
    }
  }
  return { microphone, system: true }
}

/** Rough guidance for the picker, which differs by platform in ways we cannot detect. */
export function systemAudioHint(): string {
  const platform = navigator.userAgent
  if (/Mac OS X/.test(platform)) {
    return 'On macOS, choose a **browser tab** and tick "Share tab audio" — sharing a whole screen or window captures no audio there.'
  }
  if (/Windows/.test(platform)) {
    return 'Choose a browser tab and tick "Share tab audio", or choose "Entire screen" and tick "Share system audio".'
  }
  return 'Choose a browser tab and tick "Share tab audio".'
}

/**
 * Open a display-capture stream carrying audio.
 *
 * Returns only what we use. The video track is kept alive but disabled, because
 * the capture session dies with it.
 */
export async function captureSystemAudio(): Promise<MediaStream> {
  let stream: MediaStream
  try {
    stream = await navigator.mediaDevices.getDisplayMedia({
      // Smallest video we are allowed to ask for: it is never rendered, and
      // asking for less means the browser spends less capturing it.
      video: { width: { max: 160 }, height: { max: 120 }, frameRate: { max: 1 } },
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
      // Nudge the picker towards the tab, which is the option that actually
      // carries audio on every platform.
      // @ts-expect-error — not in every lib.dom yet, ignored where unknown
      preferCurrentTab: false,
      selfBrowserSurface: 'exclude',
      systemAudio: 'include',
      surfaceSwitching: 'include',
    })
  } catch (error) {
    throw translateDisplayError(error)
  }

  if (stream.getAudioTracks().length === 0) {
    // The commonest mistake by a wide margin, and silently recording nothing
    // would be the worst possible response to it.
    stream.getTracks().forEach((track) => track.stop())
    throw new DisplayCaptureError(
      'That share has no audio.',
      `Share again and tick the audio box in the picker. ${systemAudioHint().replace(/\*\*/g, '')}`,
    )
  }

  // Keep the video track — stopping it ends the capture — but stop paying for
  // frames we will never look at.
  for (const track of stream.getVideoTracks()) {
    track.enabled = false
  }
  return stream
}

function translateDisplayError(error: unknown): DisplayCaptureError {
  const name = (error as { name?: string })?.name ?? 'Error'
  switch (name) {
    case 'NotAllowedError':
      return new DisplayCaptureError(
        'Screen sharing was cancelled or blocked.',
        'Press Record again and choose a tab, ticking the audio box.',
      )
    case 'NotFoundError':
      return new DisplayCaptureError(
        'No shareable source was found.',
        'Open the tab or app you want to capture, then try again.',
      )
    case 'NotSupportedError':
    case 'TypeError':
      return new DisplayCaptureError(
        'This browser cannot capture audio playing on this machine.',
        'Use Chrome or Edge on a desktop, or record the room with a microphone instead.',
      )
    default:
      return new DisplayCaptureError(
        `Could not capture this machine's audio (${name}).`,
        'Try again, or switch the capture source to the microphone.',
      )
  }
}

/** The track's own name, for session metadata — "Tab: Standup call". */
export function describeStream(stream: MediaStream | null): string {
  return stream?.getAudioTracks()[0]?.label ?? ''
}
