import { defineConfig } from 'vitest/config'

// Kept separate from vite.config.ts so the production build's type-check does
// not need vitest's types.
export default defineConfig({
  test: { environment: 'node', globals: true, include: ['src/**/*.test.ts'] },

})
