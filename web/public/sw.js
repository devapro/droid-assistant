/**
 * Service worker: cache the app shell (FR-UI-10, FR-CAP-17).
 *
 * The shell is cached so the page loads with the server unreachable, which is
 * what makes offline recording reachable at all — an app that will not load
 * cannot offer to record locally.
 *
 * API and WebSocket traffic is never cached. A stale transcript that looks
 * current is worse than an error.
 */

const CACHE = 'droid-shell-v1'
const SHELL = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg', '/capture-worklet.js']

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  if (event.request.method !== 'GET' || url.origin !== location.origin) return
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws/')) return

  // Network first, cache as fallback: the shell should update when the server
  // is reachable, and still load when it is not.
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone()
          void caches.open(CACHE).then((cache) => cache.put(event.request, copy))
        }
        return response
      })
      .catch(() => caches.match(event.request).then((cached) => cached ?? caches.match('/index.html'))),
  )
})
