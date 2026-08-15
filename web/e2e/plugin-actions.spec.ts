/**
 * Running a plugin on demand — over one line, and over a whole conversation.
 *
 * Two things here are worth a browser rather than a unit test:
 *
 * * the per-line control must be **reachable without hovering**. It used to sit
 *   at the far right of a full-width transcript and appear only on
 *   `group-hover`, which on a phone means it did not exist. A test that hovers
 *   first would pass against that bug, so nothing here hovers.
 * * the message shown afterwards must match what actually happened. Reporting
 *   "added" for a line holding no commitment sends the reader to a tab that
 *   does not contain what they were promised.
 */

import { expect, test, type Page } from '@playwright/test'

const RUN = '**/plugins/action_items/run'

async function recordAndOpen(page: Page): Promise<void> {
  await page.goto('/')
  await page.getByRole('button', { name: 'Start recording' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Start recording now' }).click()
  await expect(page.locator('article[data-utt]').first()).toBeVisible({ timeout: 45_000 })
  await page.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(page.getByText('Session finished.')).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: 'History' }).click()
  await page.locator('[data-session-id] button').first().click()
  await expect(page.locator('article[data-utt]').first()).toBeVisible()
}

function taskButtons(page: Page) {
  return page.locator('article[data-utt] button[aria-label*="action item"]')
}

/** Stand in for the extractor, so both outcomes are reachable on demand. */
async function answerWith(page: Page, artifact: unknown) {
  await page.unroute(RUN).catch(() => undefined)
  await page.route(RUN, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ran: true, artifact }),
    }),
  )
}

test.describe('Per-line action items', () => {
  test('the control is on every line without hovering', async ({ page }) => {
    await recordAndOpen(page)
    const buttons = taskButtons(page)
    await expect(buttons.first()).toBeVisible()
    expect(await buttons.count()).toBe(await page.locator('article[data-utt]').count())

    // Big enough to hit with a thumb. These were text-sized glyphs.
    const box = await buttons.first().boundingBox()
    expect(box!.width).toBeGreaterThanOrEqual(24)
    expect(box!.height).toBeGreaterThanOrEqual(24)
  })

  test('what it reports back is what happened', async ({ page }) => {
    await recordAndOpen(page)

    // A line the extractor found nothing in emits no artifact at all. Claiming
    // an add for it is the bug this test exists for.
    await answerWith(page, null)
    await taskButtons(page).first().click()
    await expect(page.getByText('Nothing to act on in that line.')).toBeVisible()

    await answerWith(page, { kind: 'action_items', metadata: { count: 3, added: 2 } })
    await taskButtons(page).first().click()
    await expect(page.getByText(/Added 2 action items/)).toBeVisible()
  })

  test('the machine-readable half of an artifact is not a tab', async ({ page }) => {
    await recordAndOpen(page)
    // `action_items` emits Markdown to read and JSON for anything downstream.
    // Both used to claim a tab, so the reader was offered "Action Items Json".
    await expect(page.getByRole('tab', { name: /action items/i })).toHaveCount(1)
    await expect(page.getByRole('tab', { name: /json/i })).toHaveCount(0)
  })

  test('it is reachable while the recording is still running', async ({ page }) => {
    // The case the feature exists for: a commitment is made *during* the
    // conversation. Waiting for the recording to end and then hunting for the
    // line is the work this is meant to remove.
    await page.goto('/')
    await page.getByRole('button', { name: 'Start recording' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Start recording now' }).click()
    await expect(page.locator('article[data-utt]').first()).toBeVisible({ timeout: 45_000 })

    await answerWith(page, { kind: 'action_items', metadata: { count: 1, added: 1 } })
    await expect(taskButtons(page).first()).toBeVisible()
    await taskButtons(page).first().click()
    await expect(page.getByText(/Added 1 action item/)).toBeVisible()

    await page.getByRole('button', { name: 'Stop', exact: true }).click()
  })

  test('the list is readable without stopping the recording', async ({ page }) => {
    // Collecting items line by line is only half a feature if seeing what you
    // have collected means ending the conversation first.
    await page.route('**/artifacts*', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          stale: false,
          artifacts: [
            {
              id: 'a1',
              kind: 'action_items',
              mime: 'text/markdown',
              current: true,
              content: '## Action items\n\n- [ ] Send the Q3 numbers — **Anna**',
              metadata: { count: 1 },
            },
          ],
        }),
      }),
    )
    await page.goto('/')
    await page.getByRole('button', { name: 'Start recording' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Start recording now' }).click()
    await expect(page.locator('article[data-utt]').first()).toBeVisible({ timeout: 45_000 })

    await page.getByRole('tab', { name: /Action items \(1\)/ }).click()
    await expect(page.getByText('Send the Q3 numbers')).toBeVisible()
    // …and back, without having stopped.
    await page.getByRole('tab', { name: 'Transcript' }).click()
    await expect(page.locator('article[data-utt]').first()).toBeVisible()

    await page.getByRole('button', { name: 'Stop', exact: true }).click()
  })
})

test.describe('Summary', () => {
  test('is not produced until it is asked for', async ({ page }) => {
    // Summarising costs a call over the whole transcript, and most recordings
    // are never opened twice. The tab offers a button rather than a bill.
    await recordAndOpen(page)
    await page.getByRole('tab', { name: /^summary$/i }).click()
    await expect(page.getByRole('button', { name: /Generate summary/i })).toBeVisible()
  })
})

test.describe('Marked moments', () => {
  test('one click marks a line, another clears it', async ({ page }) => {
    // FR-CAP-18 shipped as a database column: the Mark button set it, a 12 px
    // glyph showed it, no other view heard about it, and no line could be
    // marked after the fact. All three are what these assert.
    await recordAndOpen(page)
    const line = page.locator('article[data-utt]').first()
    const flag = line.locator('button[aria-label*="Mark this"]')

    await expect(flag).toBeVisible()
    await flag.click()
    await expect(page.locator('article[data-marked]')).toHaveCount(1)

    await line.locator('button[aria-label="Remove the mark"]').click()
    await expect(page.locator('article[data-marked]')).toHaveCount(0)
  })

  test('the history row says a recording holds one', async ({ page }) => {
    await recordAndOpen(page)
    await page.locator('article[data-utt] button[aria-label*="Mark this"]').first().click()
    await expect(page.locator('article[data-marked]')).toHaveCount(1)

    // The half of FR-CAP-18 that was missing: findable without opening every
    // recording in turn.
    // The detail view has its own "‹ History" back button, so name the nav one.
    await page.getByRole('button', { name: 'History', exact: true }).click()
    await expect(page.locator('[data-session-id]').first()).toContainText('1 marked')
  })
})
