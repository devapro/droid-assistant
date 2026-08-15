import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { setLocale } from './i18n'
import './styles.css'

setLocale(navigator.language)

// FR-UI-10: PWA installability, and the service worker that caches the app
// shell so it loads with the server unreachable (FR-CAP-17).
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').catch(() => undefined)
  })
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
