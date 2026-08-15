/**
 * Capture-source selection.
 *
 * The behaviour worth pinning is the failure path: `getDisplayMedia` resolves
 * happily when the user shares a tab *without* ticking the audio box, and
 * recording silence for forty minutes is the worst possible response to that.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'
import { captureSystemAudio, DisplayCaptureError, sourceSupport, systemAudioHint } from './sources'

function track(kind: 'audio' | 'video') {
  return { kind, enabled: true, stop: vi.fn(), addEventListener: vi.fn() }
}

function stream(kinds: ('audio' | 'video')[]) {
  const tracks = kinds.map(track)
  return {
    getTracks: () => tracks,
    getAudioTracks: () => tracks.filter((t) => t.kind === 'audio'),
    getVideoTracks: () => tracks.filter((t) => t.kind === 'video'),
    _tracks: tracks,
  }
}

function install(impl: unknown) {
  vi.stubGlobal('navigator', {
    mediaDevices: { getDisplayMedia: impl, getUserMedia: vi.fn() },
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
  })
}

afterEach(() => vi.unstubAllGlobals())

describe('support detection', () => {
  it('reports display capture as unavailable when the API is absent', () => {
    vi.stubGlobal('navigator', { mediaDevices: { getUserMedia: vi.fn() }, userAgent: '' })
    const support = sourceSupport()
    expect(support.system).toBe(false)
    // Naming a browser that does work is more use than "unsupported".
    expect(support.reason).toMatch(/Chrome or Edge/)
  })

  it('reports it as available when the API exists', () => {
    install(vi.fn())
    expect(sourceSupport().system).toBe(true)
  })
})

describe('picker guidance', () => {
  it('tells macOS users to share a tab, because system audio is unavailable there', () => {
    install(vi.fn())
    expect(systemAudioHint()).toMatch(/tab audio/i)
    expect(systemAudioHint()).toMatch(/whole screen or window captures no audio/i)
  })
})

describe('capturing', () => {
  it('keeps the video track alive but disabled', async () => {
    // Stopping it would end the capture session, taking the audio with it.
    const shared = stream(['audio', 'video'])
    install(vi.fn().mockResolvedValue(shared))

    const result = await captureSystemAudio()
    expect(result).toBe(shared)
    const video = shared.getVideoTracks()[0]!
    expect(video.enabled).toBe(false)
    expect(video.stop).not.toHaveBeenCalled()
  })

  it('rejects a share with no audio, and stops what it opened', async () => {
    const shared = stream(['video'])
    install(vi.fn().mockResolvedValue(shared))

    await expect(captureSystemAudio()).rejects.toThrow(DisplayCaptureError)
    // Leaving a video capture running after refusing it would keep the
    // browser's sharing banner up for a recording that never started.
    expect(shared._tracks.every((t) => t.stop.mock.calls.length === 1)).toBe(true)
  })

  it('explains how to fix a silent share', async () => {
    install(vi.fn().mockResolvedValue(stream(['video'])))
    await expect(captureSystemAudio()).rejects.toMatchObject({
      remedy: expect.stringMatching(/tick the audio box/i),
    })
  })

  it('translates a cancelled picker into something actionable', async () => {
    install(vi.fn().mockRejectedValue(Object.assign(new Error('denied'), { name: 'NotAllowedError' })))
    await expect(captureSystemAudio()).rejects.toMatchObject({
      message: expect.stringMatching(/cancelled or blocked/i),
      remedy: expect.stringMatching(/Press Record again/i),
    })
  })

  it('names a working browser when the API refuses outright', async () => {
    install(vi.fn().mockRejectedValue(Object.assign(new Error('no'), { name: 'NotSupportedError' })))
    await expect(captureSystemAudio()).rejects.toMatchObject({
      remedy: expect.stringMatching(/Chrome or Edge/),
    })
  })
})
