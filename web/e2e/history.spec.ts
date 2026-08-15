/**
 * Renaming and deleting from the history list.
 *
 * Both act on real sessions against a running server, so they also cover the
 * bit that is easy to get wrong: the list reflecting the change without a
 * refetch, and a failed rename putting the old name back.
 */

import { expect, test, type Page } from '@playwright/test'

/** Record a short session so there is something to rename or delete. */
async function makeSession(page: Page): Promise<void> {
  await page.goto('/')
  await page.getByRole('button', { name: 'Start recording' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Start recording now' }).click()
  await expect(page.locator('article[data-utt]').first()).toBeVisible({ timeout: 45_000 })
  await page.getByRole('button', { name: 'Stop', exact: true }).click()
  await expect(page.getByText('Session finished.')).toBeVisible({ timeout: 60_000 })
  await page.getByRole('button', { name: 'History' }).click()
  // The list loads asynchronously; reading a row count before it arrives
  // compares against zero and passes for the wrong reason.
  await expect(page.locator('[data-session-id]').first()).toBeVisible()
}

/**
 * Rows are addressed by session id, not by title: several recordings of the
 * same audio share a title, and a locator that cannot tell them apart tests
 * nothing useful.
 */
function firstRow(page: Page) {
  return page.locator('[data-session-id]').first()
}

function rowCount(page: Page) {
  return page.locator('[data-session-id]').count()
}

async function titleOf(page: Page) {
  return (await firstRow(page).innerText()).split('\n')[0]!.trim()
}

test.describe('History actions', () => {
  test('a recording can be renamed from the list', async ({ page }) => {
    await makeSession(page)
    const row = firstRow(page)
    const before = await titleOf(page)

    // The accessible name says which recording is being renamed, which matters
    // when several share a title.
    await expect(row.getByRole('button', { name: `Rename “${before}”` })).toBeVisible()
    await row.getByRole('button', { name: /^Rename / }).click()
    const field = firstRow(page).getByLabel('Rename recording')
    await expect(field).toBeFocused()
    await field.fill('Weekly infra sync')
    await firstRow(page).getByRole('button', { name: 'Save' }).click()

    // The row shows the new name straight away, with no refetch…
    await expect(firstRow(page)).toContainText('Weekly infra sync')
    await expect(firstRow(page).getByLabel('Rename recording')).toBeHidden()

    // …and the server agrees after a reload.
    await page.reload()
    await page.getByRole('button', { name: 'History' }).click()
    await expect(firstRow(page)).toContainText('Weekly infra sync')
  })

  test('Enter saves and Escape abandons a rename', async ({ page }) => {
    await makeSession(page)

    await firstRow(page).getByRole('button', { name: /^Rename / }).click()
    await firstRow(page).getByLabel('Rename recording').fill('Typed but abandoned')
    await firstRow(page).getByLabel('Rename recording').press('Escape')
    await expect(firstRow(page).getByLabel('Rename recording')).toBeHidden()
    await expect(page.getByText('Typed but abandoned')).toBeHidden()

    await firstRow(page).getByRole('button', { name: /^Rename / }).click()
    await firstRow(page).getByLabel('Rename recording').fill('Saved with Enter')
    await firstRow(page).getByLabel('Rename recording').press('Enter')
    await expect(firstRow(page)).toContainText('Saved with Enter')
  })

  test('an empty name cannot be saved', async ({ page }) => {
    // Clearing the field would otherwise leave a row with no identity at all.
    await makeSession(page)

    await firstRow(page).getByRole('button', { name: /^Rename / }).click()
    await firstRow(page).getByLabel('Rename recording').fill('   ')
    await expect(firstRow(page).getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  test('deleting asks for confirmation naming the recording', async ({ page }) => {
    // SRS §5.1: the confirmation names the session, because nothing is
    // recoverable afterwards (FR-SES-12).
    await makeSession(page)
    const before = await titleOf(page)
    const rowsBefore = await rowCount(page)

    await firstRow(page).getByRole('button', { name: /^Delete / }).click()
    const dialog = page.getByRole('alertdialog')
    await expect(dialog).toBeVisible()
    await expect(dialog).toContainText(before)
    await expect(dialog).toContainText(/cannot be undone/i)

    // Cancelling leaves everything alone.
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog).toBeHidden()
    expect(await rowCount(page)).toBe(rowsBefore)

    // Confirming removes it from the list and from the server.
    await firstRow(page).getByRole('button', { name: /^Delete / }).click()
    await page.getByRole('alertdialog').getByRole('button', { name: 'Delete' }).click()
    await expect(page.getByRole('alertdialog')).toBeHidden()
    await expect.poll(() => rowCount(page)).toBe(rowsBefore - 1)

    await page.reload()
    await page.getByRole('button', { name: 'History' }).click()
    await expect.poll(() => rowCount(page)).toBe(rowsBefore - 1)
  })

  test('Escape closes the confirmation without deleting', async ({ page }) => {
    await makeSession(page)
    const rowsBefore = await rowCount(page)

    await firstRow(page).getByRole('button', { name: /^Delete / }).click()
    await expect(page.getByRole('alertdialog')).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(page.getByRole('alertdialog')).toBeHidden()
    expect(await rowCount(page)).toBe(rowsBefore)
  })

  test('a failed rename restores the previous name', async ({ page }) => {
    await makeSession(page)
    const before = await titleOf(page)

    await page.route('**/api/sessions/sess_*', async (route) => {
      if (route.request().method() === 'PATCH') {
        await route.fulfill({ status: 500, json: { error: 'the server refused', component: 'sessions' } })
      } else {
        await route.continue()
      }
    })

    await firstRow(page).getByRole('button', { name: /^Rename / }).click()
    await firstRow(page).getByLabel('Rename recording').fill('This will not stick')
    await firstRow(page).getByRole('button', { name: 'Save' }).click()

    // The optimistic name is rolled back rather than left claiming something
    // the server does not agree with, and the failure is reported.
    await expect(page.getByRole('alert')).toContainText('the server refused')
    await expect(page.getByText('This will not stick')).toBeHidden()
    await expect(firstRow(page)).toContainText(before)
  })
})
