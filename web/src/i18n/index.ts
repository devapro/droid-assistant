/**
 * String lookup (FR-UI-20).
 *
 * v1 has one locale. The indirection is what makes a second one a data change:
 * add `ru.ts`, register it below, and no component changes.
 */

import { en, type Strings } from './en'

const locales: Record<string, Strings> = { en }

let active: Strings = en

export function setLocale(code: string): boolean {
  const locale = locales[code.split('-')[0] ?? 'en']
  if (!locale) return false
  active = locale
  return true
}

export function t(): Strings {
  return active
}

export const availableLocales = Object.keys(locales)
