/**
 * The capture-source selector.
 *
 * What a browser test can reach here is the surface: the option exists, it is
 * offered where the API exists, and choosing it warns about the people on the
 * other end of the call. Driving the picker itself is not automatable — Chrome
 * requires a real user choice — so `src/capture/sources.test.ts` covers the
 * stream handling instead.
 */

import { expect, test } from '@playwright/test'

async function openSetup(page: import('@playwright/test').Page) {
  await page.goto('/')
  await page.getByRole('button', { name: 'More capture options' }).click()
}

test('the source can be switched to this machine’s audio', async ({ page }) => {
  await openSetup(page)
  const source = page.getByRole('combobox').filter({ hasText: 'Microphone' }).first()
  await expect(source).toBeVisible()

  await source.selectOption('system')
  // Everyone in a call is being recorded, including people who cannot see this
  // screen — the UI says so rather than leaving it to be discovered.
  await expect(page.getByText(/your responsibility, not the software/i)).toBeVisible()

  await source.selectOption('both')
  await expect(page.getByText(/your responsibility, not the software/i)).toBeVisible()

  await source.selectOption('microphone')
  await expect(page.getByText(/your responsibility, not the software/i)).toBeHidden()
})

test('the choice survives a reload', async ({ page }) => {
  await openSetup(page)
  await page.getByRole('combobox').filter({ hasText: 'Microphone' }).first().selectOption('both')
  await page.reload()
  await page.getByRole('button', { name: 'More capture options' }).click()
  await expect(page.getByText(/your responsibility, not the software/i)).toBeVisible()
})

test('Settings offers the same choice and explains why it helps', async ({ page }) => {
  await page.goto('/#/settings')
  await expect(page.getByText('Capture from')).toBeVisible()
  await expect(page.getByText(/biggest accuracy gain available for online meetings/i)).toBeVisible()
})
