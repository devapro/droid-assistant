/**
 * Point the server at a fast model for the duration of the suite.
 *
 * These tests care that words come back, not which words, so the smallest
 * model is right — but only here. Setting it in `.env` instead once left a
 * real deployment transcribing Russian with `tiny`, which is roughly four
 * times the word error of `small`.
 */

export default async function globalSetup(): Promise<void> {
  const base = process.env.BASE_URL ?? 'http://localhost:8000'
  try {
    const response = await fetch(`${base}/api/config`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ asr_model: process.env.E2E_ASR_MODEL ?? 'tiny' }),
    })
    if (!response.ok) {
      console.warn(`could not pin the test model (${response.status}); using the server default`)
    }
  } catch {
    console.warn('server not reachable during setup; using whatever it is configured with')
  }
}
