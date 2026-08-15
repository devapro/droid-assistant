/**
 * Screen Wake Lock (FR-CAP-7, FR-CAP-8).
 *
 * The single most important mitigation for browser-only capture: without it a
 * phone locks its screen after a minute and the recording stops. The common
 * alternative — playing a silent audio loop — is unreliable and burns battery,
 * and is not used here.
 *
 * The lock is released by the browser whenever the tab is hidden, so it must be
 * re-acquired on `visibilitychange`. Missing that is why "it stopped when I
 * checked a message" is the classic bug in this class of app.
 */

export type WakeLockState = 'held' | 'released' | 'unsupported' | 'denied'

export class WakeLock {
  private sentinel: WakeLockSentinel | null = null
  private wanted = false
  private listening = false

  constructor(private readonly onChange?: (state: WakeLockState) => void) {}

  get supported(): boolean {
    return 'wakeLock' in navigator
  }

  get held(): boolean {
    return this.sentinel !== null && !this.sentinel.released
  }

  async acquire(): Promise<WakeLockState> {
    this.wanted = true
    if (!this.supported) {
      this.onChange?.('unsupported')
      return 'unsupported'
    }
    try {
      this.sentinel = await navigator.wakeLock.request('screen')
      this.sentinel.addEventListener('release', () => {
        this.onChange?.('released')
        // A release while we still want the lock means the tab was backgrounded.
        // Re-acquisition happens on the visibility event, not here — requesting
        // one while hidden is rejected.
      })
      this.listen()
      this.onChange?.('held')
      return 'held'
    } catch {
      // Denied by policy, or the document is not visible.
      this.onChange?.('denied')
      return 'denied'
    }
  }

  private listen(): void {
    if (this.listening) return
    this.listening = true
    document.addEventListener('visibilitychange', this.onVisibility)
  }

  private onVisibility = async (): Promise<void> => {
    if (this.wanted && document.visibilityState === 'visible' && !this.held) {
      await this.acquire()
    }
  }

  async release(): Promise<void> {
    this.wanted = false
    if (this.listening) {
      document.removeEventListener('visibilitychange', this.onVisibility)
      this.listening = false
    }
    try {
      await this.sentinel?.release()
    } catch {
      // Already released by the browser.
    }
    this.sentinel = null
  }
}

/** Copy for the notice shown when no lock is available (FR-CAP-8). */
export const WAKE_LOCK_WARNING =
  'This browser cannot keep the screen awake. Keep the screen on and this tab in front, ' +
  'or recording will stop when the screen locks.'
