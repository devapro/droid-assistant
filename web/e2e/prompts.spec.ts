/**
 * Saved prompts (FR-PLG-14) — written in Settings, chosen where the work is asked for.
 *
 * Two things here need a browser rather than a unit test:
 *
 * * **the round trip through the editor.** A prompt that saves but comes back
 *   empty on the next visit is indistinguishable from one that never saved, and
 *   the difference lives in a form, a PUT, and a reload — not in one function.
 * * **the picker has to reach the request.** A select that renders, changes, and
 *   then posts nothing is the worst version of this feature: it looks like a
 *   choice and produces the built-in summary anyway. So the run is intercepted
 *   and its body asserted.
 *
 * Both assume a deployment whose `summary` plugin is available — that is, one
 * with an LLM configured. Without it the plugin reports unavailable, no tab is
 * offered, and there is nowhere for a picker to be.
 */

import { expect, test, type Page } from '@playwright/test'

const NAME = 'E2E customer call'
const INSTRUCTIONS = 'Lead with what the customer asked for, then what we promised.'
const RUN = '**/plugins/summary/run'

async function openPrompts(page: Page): Promise<void> {
  await page.goto('/')
  await page.getByRole('button', { name: 'Settings' }).click()
  await page.getByRole('tab', { name: 'Prompts' }).click()
}

async function saved(page: Page): Promise<{ id: string; name: string }[]> {
  const listed = await page.request.get('/api/prompts')
  return listed.ok() ? ((await listed.json()) as { prompts: [] }).prompts : []
}

/** Remove the prompt this suite writes, however the test that made it ended. */
async function removePrompt(page: Page): Promise<void> {
  for (const prompt of (await saved(page)).filter((p) => p.name === NAME)) {
    await page.request.delete(`/api/prompts/${prompt.id}`)
  }
}

test.describe('Prompts', () => {
  test.afterEach(async ({ page }) => {
    await removePrompt(page)
  })

  test('a prompt written in Settings survives a reload', async ({ page }) => {
    await openPrompts(page)

    const draft = page.locator('[data-prompt="new"]')
    await page.getByRole('button', { name: 'New prompt' }).click()
    await draft.getByRole('textbox', { name: /Name it/ }).fill(NAME)
    await draft.getByRole('textbox', { name: 'Instructions' }).fill(INSTRUCTIONS)
    await draft.getByRole('button', { name: 'Save', exact: true }).click()

    const id = (await saved(page)).find((prompt) => prompt.name === NAME)?.id
    expect(id).toBeTruthy()

    // Not "the input still holds what I typed" — that is true of a save that
    // never happened. The value has to come back from the server.
    await openPrompts(page)
    const card = page.locator(`[data-prompt="${id}"]`)
    await expect(card.getByRole('textbox', { name: /Name it/ })).toHaveValue(NAME)
    await expect(card.getByRole('textbox', { name: 'Instructions' })).toHaveValue(INSTRUCTIONS)

    // Save is offered only while there is something unsaved. A button that sits
    // there afterwards cannot be told apart from one that did nothing.
    await expect(card.getByRole('button', { name: 'Save', exact: true })).toHaveCount(0)
  })

  test('deleting one is confirmed, and it goes', async ({ page }) => {
    const created = await page.request.put('/api/prompts', {
      data: { name: NAME, instructions: INSTRUCTIONS },
    })
    const { id } = (await created.json()) as { id: string }
    await openPrompts(page)

    const card = page.locator(`[data-prompt="${id}"]`)
    await card.getByRole('button', { name: 'Delete' }).click()
    const dialog = page.getByRole('alertdialog')
    await expect(dialog).toContainText(NAME)
    await dialog.getByRole('button', { name: 'Delete' }).click()

    await expect(card).toHaveCount(0)
  })

  test('the picker sits beside Generate and reaches the request', async ({ page }) => {
    const created = await page.request.put('/api/prompts', {
      data: { name: NAME, instructions: INSTRUCTIONS },
    })
    const prompt = (await created.json()) as { id: string }

    // A recording to generate over. Batch would do as well; what matters is a
    // finished session with a summary tab nobody has generated yet.
    await page.goto('/')
    await page.getByRole('button', { name: 'Start recording' }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Start recording now' }).click()
    await expect(page.locator('article[data-utt]').first()).toBeVisible({ timeout: 45_000 })
    await page.getByRole('button', { name: 'Stop', exact: true }).click()
    await expect(page.getByText('Session finished.')).toBeVisible({ timeout: 60_000 })
    await page.getByRole('button', { name: 'History' }).click()
    await page.locator('[data-session-id] button').first().click()

    await page.getByRole('tab', { name: 'summary' }).click()
    const picker = page.getByLabel('Prompt')
    await expect(picker).toBeVisible()
    // The built-in prompt is the default, and is an option rather than a row in
    // Settings: it cannot be edited or deleted.
    await expect(picker).toHaveValue('')
    await picker.selectOption({ label: NAME })

    // Stand in for the model: this test is about what was *asked for*, not what
    // came back — a real run costs a call over the whole transcript, and the
    // panel that renders the result reads it back from the session rather than
    // from this response, so faking one further would prove nothing.
    let body: Record<string, unknown> = {}
    await page.route(RUN, async (route) => {
      body = JSON.parse(route.request().postData() ?? '{}')
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ran: true,
          artifact: {
            id: 'art_e2e',
            plugin_name: 'summary',
            kind: 'summary',
            mime: 'text/markdown',
            content: 'They asked for onboarding by Friday.',
            version: 1,
            current: true,
            created_at: Date.now(),
            metadata: { prompt: NAME },
          },
        }),
      })
    })

    await page.getByRole('button', { name: /Generate/ }).click()
    await expect.poll(() => body.prompt_id).toBe(prompt.id)
  })
})
