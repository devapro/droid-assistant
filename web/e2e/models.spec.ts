/**
 * Settings → Speech models (FR-ASR-1, FR-ASR-7).
 *
 * The behaviour under test is the one the config file could never give you:
 * choosing a *different model per language* without a restart, and only ever
 * being offered models this server actually has.
 *
 * `/api/models` is mocked. The real one depends on which weights happen to be
 * on the machine running the suite, and a test that passes only on a
 * fully-populated models directory is not a test. The PATCH is captured rather
 * than mocked away, because what the client sends is the thing that matters.
 */

import { expect, test, type Page } from '@playwright/test'

const MODELS = {
  models: [
    {
      id: 'faster_whisper:large-v3-turbo',
      backend: 'faster_whisper',
      model: 'large-v3-turbo',
      state: 'present',
      local: true,
      size_mb: 2298,
      note: 'the default: best accuracy per second of compute',
      languages: ['en', 'ru', 'sr'],
      error: null,
    },
    {
      id: 'gigaam:v3-rnnt',
      backend: 'gigaam',
      model: 'v3-rnnt',
      state: 'present',
      local: true,
      size_mb: 219,
      note: 'Russian-only, and far faster than Whisper at comparable accuracy',
      languages: ['ru'],
      error: null,
    },
    {
      id: 'faster_whisper:small',
      backend: 'faster_whisper',
      model: 'small',
      state: 'absent',
      local: true,
      size_mb: 484,
      note: 'good CPU compromise',
      languages: ['en', 'ru', 'sr'],
      error: null,
    },
    {
      id: 'openai:whisper-1',
      backend: 'openai',
      model: 'whisper-1',
      state: 'unavailable',
      local: false,
      size_mb: 0,
      note: 'OpenAI, word timestamps',
      languages: null,
      error: null,
    },
  ],
  default: 'faster_whisper:large-v3-turbo',
  routing: {
    en: { id: 'faster_whisper:large-v3-turbo', explicit: false },
    ru: { id: 'faster_whisper:large-v3-turbo', explicit: false },
  },
  languages: ['en', 'ru'],
  models_dir: '/models',
  note: 'A model change applies to the next session.',
}

async function openModels(page: Page, body: unknown = MODELS) {
  await page.route('**/api/models', (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
  })
  await page.goto('/#/settings')
  await page.getByRole('tab', { name: 'Speech models' }).click()
}

test('offers only the models this server has, per language', async ({ page }) => {
  await openModels(page)

  const russian = page.locator('select').nth(2)
  const options = await russian.locator('option').allTextContents()

  // GigaAM is Russian-only and downloaded, so it belongs here.
  expect(options.some((o) => o.includes('gigaam · v3-rnnt'))).toBe(true)
  // `small` is not on disk. Selecting it would route Russian to weights that
  // are not there, and the session would fail at the moment of recording.
  expect(options.some((o) => o.includes('faster-whisper · small'))).toBe(false)
  // No credential, so it cannot be used whatever the routing says.
  expect(options.some((o) => o.includes('whisper-1'))).toBe(false)
  // Falling back to the default is a choice, and it has to be nameable.
  expect(options[0]).toBe('Use default')
})

test('a Russian-only model is not offered for English', async ({ page }) => {
  await openModels(page)

  const english = page.locator('select').nth(1)
  const options = await english.locator('option').allTextContents()
  // Handed English, GigaAM does not fail — it returns fluent nonsense. Not
  // offering it is the only place this can be prevented.
  expect(options.some((o) => o.includes('gigaam'))).toBe(false)
})

test('choosing a model for one language patches only that language', async ({ page }) => {
  const sent: unknown[] = []
  await page.route('**/api/config', async (route) => {
    if (route.request().method() !== 'PATCH') return route.continue()
    sent.push(route.request().postDataJSON())
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ config: {}, applies_to: 'the next session' }),
    })
  })
  await openModels(page)

  await page.locator('select').nth(2).selectOption('gigaam:v3-rnnt')

  await expect.poll(() => sent).toEqual([{ asr_by_language: { ru: 'gigaam:v3-rnnt' } }])
  // The server's own words about when this takes effect, echoed back — not the
  // static help text, which says something similar above every control.
  await expect(page.getByText('the next session', { exact: true })).toBeVisible()
})

test('an absent model offers a download, and says how large it is', async ({ page }) => {
  let requested: string | null = null
  await page.route('**/api/models/download', async (route) => {
    requested = route.request().postDataJSON().id
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({ id: requested, state: 'downloading' }),
    })
  })
  await openModels(page)

  const row = page.locator('div').filter({ hasText: /^faster-whisper · small/ }).first()
  await expect(row).toContainText('not downloaded')

  await page.getByRole('button', { name: /Download.*484 MB/ }).click()
  await expect.poll(() => requested).toBe('faster_whisper:small')
})

test('the models directory is shown, so a missing model can be traced', async ({ page }) => {
  await openModels(page)
  await expect(page.getByText('/models', { exact: false }).last()).toBeVisible()
})
