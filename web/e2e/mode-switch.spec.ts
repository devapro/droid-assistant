/**
 * The latency-mode selector, during a recording and before one.
 *
 * It stays enabled while recording on purpose — FR-LAT-3 makes the mode
 * changeable mid-session, and the server applies it at the next VAD boundary.
 * What it must not do is offer a mode the recogniser cannot serve, or claim a
 * switch the server refused.
 */

import { expect, test, type Page } from '@playwright/test'

function selector(page: Page) {
  return page.getByLabel('Latency mode')
}

async function startRecording(page: Page): Promise<void> {
  await page.goto('/')
  await page.getByRole('button', { name: 'Start recording' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Start recording now' }).click()
  await expect(page.getByTestId('recording-indicator')).toBeVisible()
}

test.describe('Latency mode', () => {
  test('stays changeable while recording', async ({ page }) => {
    await startRecording(page)
    await expect(selector(page)).toBeEnabled()
    await selector(page).selectOption('batch')
    await expect(selector(page)).toHaveValue('batch')
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
  })

  test('a mode the recogniser cannot serve is not offered', async ({ page }) => {
    // The check read `requires_streaming_backend && !emits_partials`, which is
    // never true — the only mode needing a streaming recogniser is the only one
    // emitting partials. So Live was always selectable and always failed.
    await page.route('**/api/modes', async (route) => {
      const body = await (await route.fetch()).json()
      route.fulfill({ json: { ...body, streaming_backend: false } })
    })
    await page.goto('/')
    await expect(selector(page).locator('option[value="live"]')).toBeDisabled()
    await expect(selector(page).locator('option[value="balanced"]')).toBeEnabled()
  })

  test('a refused switch does not leave the selector lying', async ({ page }) => {
    // The mode was applied locally before the request and nothing awaited the
    // rejection, so the selector showed a mode the session was not in.
    //
    // Live has to be *selectable* for this to be reachable at all, so the
    // recogniser is claimed to stream and the switch is refused anyway — which
    // is also the honest case: a backend can stop streaming between the page
    // loading and the switch.
    await page.route('**/api/modes', async (route) => {
      const body = await (await route.fetch()).json()
      route.fulfill({ json: { ...body, streaming_backend: true } })
    })
    await startRecording(page)
    await page.route('**/mode', (route) =>
      route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'Live mode needs a streaming ASR backend' }),
      }),
    )
    await selector(page).selectOption('live')

    await expect(page.getByText(/needs a streaming ASR backend/)).toBeVisible()
    await expect(selector(page)).toHaveValue('balanced')
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
  })
})
