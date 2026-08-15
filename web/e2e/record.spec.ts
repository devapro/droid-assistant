/**
 * End-to-end through a real browser against a running server.
 *
 * Chrome's fake capture device plays a WAV of real speech into
 * `getUserMedia`, so this exercises the whole client path that no other test
 * can reach: the AudioWorklet, the resampler, the chunker, the IndexedDB
 * buffer, the WebSocket, and the live event stream.
 */

import { expect, test, type Page } from '@playwright/test'

/** Give VAD, recognition, and delivery time to produce a first line. */
const FIRST_UTTERANCE_MS = 45_000

async function record(page: Page, seconds: number): Promise<void> {
  await page.getByRole('button', { name: 'Start recording' }).click()
  // The pre-flight check runs before capture starts (FR-UI-18).
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: 'Start recording now' }).click()
  await expect(page.getByTestId('recording-indicator')).toHaveText('REC')
  await page.waitForTimeout(seconds * 1000)
}

test.describe('Recording', () => {
  test('the landing view is Record, one tap from recording', async ({ page }) => {
    // FR-UI-12: cold-loading leaves Record one tap away.
    await page.goto('/')
    await expect(page.getByRole('button', { name: 'Start recording' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Record' }).first()).toBeVisible()
    await expect(page.getByRole('button', { name: 'History' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Settings' })).toBeVisible()
  })

  test('pre-flight reports the server and the microphone before starting', async ({ page }) => {
    // FR-UI-18: never discover after forty minutes that the mic was muted.
    await page.goto('/')
    await page.getByRole('button', { name: 'Start recording' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText('Server reachable')).toBeVisible()
    await expect(dialog.getByText('Microphone working')).toBeVisible()
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog).toBeHidden()
  })

  test('speech becomes a live, speaker-attributed transcript', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))

    await page.goto('/')
    await record(page, 14)

    // The recording indicator is present and undismissable while capturing
    // (FR-UI-3), and the link state is shown (FR-UI-6).
    await expect(page.getByTestId('recording-indicator')).toHaveText('REC')
    await expect(page.getByText(/Connected/)).toBeVisible()

    // A finalised utterance arrives without a page refresh (FR-UI-1).
    const transcript = page.locator('article[data-utt]')
    await expect(transcript.first()).toBeVisible({ timeout: FIRST_UTTERANCE_MS })
    const text = await transcript.first().innerText()
    expect(text.toLowerCase()).toContain('migration')

    // Every finalised utterance carries a speaker label, not only a colour
    // (FR-UI-16, FR-DIA-10).
    await expect(transcript.first().getByText(/Speaker \d/)).toBeVisible()

    await page.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(page.getByText('Session finished.')).toBeVisible({ timeout: 60_000 })
    expect(errors).toEqual([])
  })

  test('pause and resume keep one session', async ({ page }) => {
    // FR-CAP-16: breaks and side conversations are routine.
    await page.goto('/')
    await record(page, 3)
    await page.getByRole('button', { name: 'Pause' }).click()
    await expect(page.getByTestId('recording-indicator')).toHaveText('Paused')
    await page.getByRole('button', { name: 'Resume' }).click()
    await expect(page.getByTestId('recording-indicator')).toHaveText('REC')
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(page.getByText('Session finished.')).toBeVisible({ timeout: 60_000 })
  })

  test('batch mode shows no transcript while recording', async ({ page }) => {
    // FR-LAT-6: only elapsed time and metering appear, with copy that says why.
    await page.goto('/')
    await page.getByLabel('Latency mode').selectOption('batch')
    await record(page, 4)
    await expect(page.getByText(/Batch mode transcribes after you stop/)).toBeVisible()
    await expect(page.locator('article[data-utt]')).toHaveCount(0)
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(page.getByText('Session finished.')).toBeVisible({ timeout: 90_000 })
  })
})

test.describe('History and session detail', () => {
  test('a recorded session is findable, playable, and editable', async ({ page }) => {
    await page.goto('/')
    await record(page, 12)
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(page.getByText('Session finished.')).toBeVisible({ timeout: 60_000 })

    await page.getByRole('button', { name: 'History' }).click()
    const rows = page.locator('button', { hasText: /speaker/ })
    await expect(rows.first()).toBeVisible()

    // FR-SES-10: search returns matches, highlighted. The count is not asserted
    // because the server keeps every session ever recorded against it — the
    // requirement is that matches appear and are marked, not how many.
    await page.getByPlaceholder(/Search transcripts/).fill('migration')
    await expect(page.locator('mark').first()).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('mark').first()).toHaveText(/migration/i)

    // Selecting a match opens the session (FR-SES-13).
    await page.locator('mark').first().click()
    await expect(page.getByRole('tab', { name: 'Transcript' })).toBeVisible()

    // FR-SES-8: editing marks the line as edited and keeps the original.
    const line = page.locator('article[data-utt]').first()
    await line.hover()
    await line.getByRole('button', { name: 'Edit this line' }).click()
    await page.locator('textarea').fill('corrected by the browser test')
    await page.getByRole('button', { name: 'Save' }).click()
    await expect(page.getByText('edited')).toBeVisible()
    await expect(page.getByText('corrected by the browser test')).toBeVisible()

    // FR-UI-8: a player synchronised with the transcript.
    await expect(page.getByRole('button', { name: 'Play' })).toBeVisible()
  })
})

test.describe('Layout', () => {
  test('no horizontal scrolling at any width', async ({ page }) => {
    // FR-UI-5: verified at 375 px, and nothing wider should regress it.
    await page.goto('/')
    for (const width of [375, 768, 1280]) {
      await page.setViewportSize({ width, height: 700 })
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
      )
      expect(overflow, `horizontal overflow at ${width}px`).toBe(false)
    }
  })

  test('empty states say what to do next', async ({ page }) => {
    // FR-UI-15: a blank area reads as a bug.
    await page.goto('/#/history')
    await page.getByPlaceholder(/Search transcripts/).fill('zzzznothingmatchesthis')
    await expect(page.getByText(/Nothing matches/)).toBeVisible({ timeout: 15_000 })
    await expect(page.getByText(/Clear the filters/)).toBeVisible()
  })
})
