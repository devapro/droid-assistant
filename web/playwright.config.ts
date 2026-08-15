import { defineConfig, devices } from '@playwright/test'

/**
 * Browser tests (SRS §14 — "Browser: permission flow, reconnect, autoscroll
 * pin, wake lock, pause").
 *
 * These run against a *running server*, not a mock: `BASE_URL` defaults to the
 * Docker deployment on localhost. The point is to exercise the real capture
 * path — getUserMedia → AudioWorklet → resample → WebSocket — which no unit
 * test can reach.
 *
 * Chrome's fake capture device replaces the microphone with a WAV file, so the
 * audio that arrives at the server is real speech that travelled through the
 * real client code. `localhost` is a secure context, so `getUserMedia` is
 * permitted without HTTPS.
 */
export default defineConfig({
  testDir: './e2e',
  // Recognition is not instant, and these assertions wait on a real model.
  timeout: 120_000,
  expect: { timeout: 45_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: process.env.BASE_URL ?? 'http://localhost:8000',
    trace: 'retain-on-failure',
    video: 'off',
    permissions: ['microphone'],
    launchOptions: {
      args: [
        '--use-fake-ui-for-media-stream',
        '--use-fake-device-for-media-stream',
        `--use-file-for-fake-audio-capture=${new URL('./e2e/fixtures/conversation.wav', import.meta.url).pathname}`,
        '--autoplay-policy=no-user-gesture-required',
      ],
    },
  },
  projects: [
    // Desktop: the transcript takes the width beside a persistent rail.
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    // 375 px is the width FR-UI-4 and FR-UI-5 are specified against.
    { name: 'phone', use: { ...devices['Pixel 7'], viewport: { width: 375, height: 667 } } },
  ],
})
