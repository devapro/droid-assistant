import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { resolve } from 'node:path'

// The client builds straight into the Python package's static directory, so the
// server ships one artefact and one port (SRS §6.2).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: resolve(__dirname, '../src/droid_assistant/static'),
    emptyOutDir: true,
    // NFR-RES-6 budget is 500 KB gzipped; warn well before it.
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        // The AudioWorklet is loaded by URL at runtime and must not be inlined
        // or renamed into a hashed chunk the worklet loader cannot find.
        manualChunks: { react: ['react', 'react-dom'] },
      },
    },
  },
  worker: { format: 'es' },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
})
