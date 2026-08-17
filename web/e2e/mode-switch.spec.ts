/**
 * The latency-mode selector, during a recording and before one.
 *
 * It stays enabled while recording on purpose — FR-LAT-3 makes the mode
 * changeable mid-session, and the server applies it at the next VAD boundary.
 * What it must not do is hide a mode the recogniser can serve, or claim a
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

  test('every mode is offered, whatever the recogniser', async ({ page }) => {
    // Live was greyed out for any recogniser without a streaming API, which is
    // every local one — while the server had all along been serving Live on
    // exactly those, by re-decoding a sliding window. The option is real.
    await page.goto('/')
    await expect(selector(page).locator('option[value="live"]')).toBeEnabled()
    await expect(selector(page).locator('option[value="balanced"]')).toBeEnabled()
    await selector(page).selectOption('live')
    await expect(selector(page)).toHaveValue('live')
  })

  test('what Live costs is said where it is chosen', async ({ page }) => {
    // The mode is available but not free: it recognises the same audio several
    // times a second. Against a local recogniser that is the one thing worth
    // knowing before picking it (FR-LAT-7).
    await page.goto('/')
    await expect(selector(page).locator('option[value="live"]')).toContainText(
      /re-recognises continuously/,
    )
    await expect(selector(page).locator('option[value="batch"]')).not.toContainText(
      /re-recognises continuously/,
    )
  })

  test('a refused switch does not leave the selector lying', async ({ page }) => {
    // The mode was applied locally before the request and nothing awaited the
    // rejection, so the selector showed a mode the session was not in.
    await startRecording(page)
    await page.route('**/mode', (route) =>
      route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'this session is not running' }),
      }),
    )
    await selector(page).selectOption('live')

    await expect(page.getByText(/this session is not running/)).toBeVisible()
    await expect(selector(page)).toHaveValue('balanced')
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
  })
})
