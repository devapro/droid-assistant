/**
 * What Settings shows when the server cannot answer, or is not ready yet.
 *
 * Both states are mocked rather than timed: the real ones depend on whether a
 * multi-gigabyte download happens to be in flight, and a test that only passes
 * during someone's first run is not a test.
 *
 * The defect these cover: a failed `/api/health` was swallowed, leaving every
 * panel showing "Loading…" for ever — indistinguishable from a hang.
 */

import { expect, test, type Page } from '@playwright/test'

async function healthReturns(page: Page, body: unknown) {
  await page.route('**/api/health', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }),
  )
}

const LOADING = {
  status: 'starting',
  models: {
    state: 'loading',
    backend: 'faster-whisper:large-v3-turbo',
    error: null,
    detail: 'The speech model is loading. On a first run this downloads several gigabytes.',
  },
  version: '1.0.0.dev0',
  uptime_s: 4,
  backends: {
    asr: { backend: 'faster_whisper', name: 'faster-whisper:large-v3-turbo', streaming: false, local: true },
    diarization: { name: 'sherpa-onnx', local: true },
    translation: { name: 'llm:', local: true, available: false },
    llm: { model: '', local: true, available: false },
  },
  gpu: { present: false, cuda_devices: 0 },
  disk: { available: true, free_mb: 100_000, total_mb: 500_000 },
  local_only: false,
  active_sessions: [],
  plugins: [],
  errors: [],
  warnings: [],
}

test('a loading model is reported, not hidden behind a spinner', async ({ page }) => {
  await healthReturns(page, LOADING)
  await page.goto('/#/settings')
  await page.getByRole('tab', { name: 'Backends' }).click()

  await expect(page.getByText('The speech model is still loading.')).toBeVisible()
  await expect(page.getByText(/downloads several gigabytes/i)).toBeVisible()
  // The backend cards still render — the server is answering, just not ready.
  await expect(page.getByText('Speech recognition')).toBeVisible()

  // And it appears as a first-class fact beside disk and GPU.
  await page.getByRole('tab', { name: 'Server' }).click()
  await expect(page.getByText('Speech model', { exact: true })).toBeVisible()
  await expect(page.getByText(/large-v3-turbo · loading/)).toBeVisible()
})

test('an unreachable server says so once, and offers retry', async ({ page }) => {
  await page.route('**/api/health', (route) => route.abort())
  await page.goto('/#/settings')
  await page.getByRole('tab', { name: 'Backends' }).click()

  // Once, not once per section.
  await expect(page.getByText(/Cannot reach the server/i)).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible()
  // The old behaviour, which read as a hang.
  await expect(page.getByText('Loading…')).toBeHidden()
})

test('retry recovers once the server answers', async ({ page }) => {
  let reachable = false
  await page.route('**/api/health', (route) =>
    reachable
      ? route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ...LOADING, status: 'ok', models: { ...LOADING.models, state: 'ready' } }),
        })
      : route.abort(),
  )

  await page.goto('/#/settings')
  await page.getByRole('tab', { name: 'Backends' }).click()
  await expect(page.getByText(/Cannot reach the server/i)).toBeVisible()

  reachable = true
  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText(/Cannot reach the server/i)).toBeHidden()
  await expect(page.getByText('Speech recognition')).toBeVisible()
})
