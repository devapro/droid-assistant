/**
 * Paging the history list (SRS §5.1).
 *
 * The defect these cover: the list fetched a flat 100 sessions and stopped,
 * with nothing on screen saying so. Past that, older recordings were
 * unreachable — and indistinguishable from not existing.
 *
 * `/api/sessions` is mocked so the assertions do not depend on how many
 * recordings the machine running the suite happens to have.
 */

import { expect, test, type Page } from '@playwright/test'

function session(index: number) {
  return {
    id: `sess_${String(index).padStart(4, '0')}`,
    title: `Recording ${index}`,
    state: 'ended',
    started_at: 1_700_000_000_000 - index * 60_000,
    ended_at: 1_700_000_060_000 - index * 60_000,
    duration_ms: 60_000,
    mode: 'balanced',
    source_languages: ['en'],
    target_language: 'en',
    vocabulary: [],
    cloud_used: false,
    providers_used: [],
    cost_usd: 0,
    cost_breakdown: {},
    local_only: false,
    has_audio: true,
    audio_duration_ms: 60_000,
    tags: [],
    participants: [],
    dropped_chunks: 0,
    speaker_count: 2,
    artifact_kinds: [],
    utterance_count: 10,
    live: false,
  }
}

const TOTAL = 120

/** Serve `TOTAL` sessions, honouring limit/offset like the real endpoint. */
async function servePages(page: Page, requests: string[] = []) {
  await page.route('**/api/sessions?*', (route) => {
    const url = new URL(route.request().url())
    if (route.request().method() !== 'GET') return route.continue()
    requests.push(url.search)
    const limit = Number(url.searchParams.get('limit') ?? 50)
    const offset = Number(url.searchParams.get('offset') ?? 0)
    const slice = Array.from({ length: TOTAL }, (_, i) => session(i)).slice(offset, offset + limit)
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        sessions: slice,
        total: TOTAL,
        limit,
        offset,
        has_more: offset + slice.length < TOTAL,
        facets: { languages: ['en', 'ru'], tags: ['standup'] },
      }),
    })
  })
  await page.goto('/#/history')
}

test('says how many recordings there are, not just how many are shown', async ({ page }) => {
  await servePages(page)
  // The whole point: a truncated list must be distinguishable from a short one.
  await expect(page.getByTestId('session-count')).toHaveText(`50 of ${TOTAL}`)
})

test('loads the next page and counts down what remains', async ({ page }) => {
  const requests: string[] = []
  await servePages(page, requests)

  await expect(page.getByRole('button', { name: 'Load 70 more recordings' })).toBeVisible()
  await page.getByRole('button', { name: 'Load 70 more recordings' }).click()

  await expect(page.getByTestId('session-count')).toHaveText(`100 of ${TOTAL}`)
  await expect(page.getByRole('button', { name: 'Load 20 more recordings' })).toBeVisible()
  // The second request must actually be offset, not a repeat of the first.
  expect(requests.some((search) => search.includes('offset=50'))).toBe(true)
})

test('reaching the end removes the control rather than looping', async ({ page }) => {
  await servePages(page)
  for (const remaining of [70, 20]) {
    await page.getByRole('button', { name: `Load ${remaining} more recordings` }).click()
  }
  await expect(page.getByTestId('session-count')).toHaveText(String(TOTAL))
  await expect(page.getByRole('button', { name: /Load .* more/ })).toHaveCount(0)
})

test('every row is distinct across a page boundary', async ({ page }) => {
  await servePages(page)
  await page.getByRole('button', { name: 'Load 70 more recordings' }).click()
  await expect(page.getByTestId('session-count')).toHaveText(`100 of ${TOTAL}`)

  const ids = await page.locator('[data-session-id]').evaluateAll((nodes) =>
    nodes.map((node) => node.getAttribute('data-session-id')),
  )
  expect(ids.length).toBe(100)
  expect(new Set(ids).size).toBe(100)
})

test('changing a filter starts again from the first page', async ({ page }) => {
  const requests: string[] = []
  await servePages(page, requests)
  await page.getByRole('button', { name: 'Load 70 more recordings' }).click()
  await expect(page.getByTestId('session-count')).toHaveText(`100 of ${TOTAL}`)

  requests.length = 0
  await page.getByLabel('All languages').selectOption('ru')

  await expect(page.getByTestId('session-count')).toHaveText(`50 of ${TOTAL}`)
  // Filtering must not keep the old offset, or the first 50 matches are skipped.
  expect(requests.every((search) => !search.includes('offset=50'))).toBe(true)
})

test('filter options come from every session, not just the loaded page', async ({ page }) => {
  await servePages(page)
  // `ru` and `standup` appear on no loaded row; they come from the server's
  // facets. Building these lists client-side meant a filter could not reach
  // the thing it existed to find.
  await expect(page.getByLabel('All languages').locator('option')).toContainText(['RU'])
  await expect(page.getByLabel('Tags').locator('option')).toContainText(['#standup'])
})
