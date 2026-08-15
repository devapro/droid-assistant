/**
 * The application shell.
 *
 * Three top-level destinations, flat, each one interaction from each other, and
 * Record is the landing view (FR-UI-12). On phones they are a bottom tab bar
 * inside thumb reach; on desktop a left rail with the transcript taking the
 * remaining width (SRS §5.1).
 *
 * Routing is a hash and forty lines rather than a router dependency: four
 * screens do not justify the bundle, and NFR-RES-6 caps it at 500 KB gzipped.
 */

import { useEffect, useState } from 'react'
import { History } from './views/History'
import { Record } from './views/Record'
import { SessionDetail } from './views/SessionDetail'
import { Settings } from './views/Settings'
import { useRecording } from './state/recording'
import { t } from './i18n'
import { listPendingSessions } from './capture/buffer'

type Route =
  | { name: 'record' }
  | { name: 'history' }
  | { name: 'settings' }
  | { name: 'session'; id: string; utteranceId?: string }

function parseHash(): Route {
  const hash = location.hash.replace(/^#\/?/, '')
  const [head, id, utteranceId] = hash.split('/')
  switch (head) {
    case 'history':
      return { name: 'history' }
    case 'settings':
      return { name: 'settings' }
    case 'session':
      return id ? { name: 'session', id, utteranceId } : { name: 'history' }
    default:
      return { name: 'record' }
  }
}

function navigate(route: Route): void {
  switch (route.name) {
    case 'session':
      location.hash = `#/session/${route.id}${route.utteranceId ? `/${route.utteranceId}` : ''}`
      break
    default:
      location.hash = `#/${route.name}`
  }
}

export function App() {
  const strings = t()
  const [route, setRoute] = useState<Route>(parseHash)
  const [pendingOffline, setPendingOffline] = useState(0)
  const recordState = useRecording((state) => state.state)
  const recording = recordState === 'recording'

  useEffect(() => {
    const onHashChange = () => setRoute(parseHash())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  useEffect(() => {
    const stored = localStorage.getItem('droid.theme')
    if (stored && stored !== 'system') document.documentElement.dataset.theme = stored
  }, [])

  // FR-CAP-17: recordings made while the server was down are waiting to upload.
  useEffect(() => {
    void listPendingSessions().then((sessions) => setPendingOffline(sessions.length))
  }, [route])

  // FR-UI-3: leaving the page mid-recording is almost never intended.
  useEffect(() => {
    if (!recording) return
    const warn = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [recording])

  const destinations: { id: Route['name']; label: string; icon: string }[] = [
    { id: 'record', label: strings.nav.record, icon: '●' },
    { id: 'history', label: strings.nav.history, icon: '☰' },
    { id: 'settings', label: strings.nav.settings, icon: '⚙' },
  ]

  const active = route.name === 'session' ? 'history' : route.name

  return (
    <div className="bg-surface-0 text-fg flex h-[100dvh] flex-col sm:flex-row">
      {/* Desktop rail */}
      <nav className="border-line hidden w-44 shrink-0 flex-col gap-1 border-r p-3 sm:flex">
        <p className="text-fg-dim mb-3 px-2 text-xs font-semibold tracking-wider uppercase">
          droid-assistant
        </p>
        {destinations.map((destination) => (
          <button
            key={destination.id}
            type="button"
            onClick={() => navigate({ name: destination.id } as Route)}
            aria-current={active === destination.id ? 'page' : undefined}
            className={`flex items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition ${
              active === destination.id ? 'bg-surface-2 text-fg' : 'text-fg-dim hover:bg-surface-2'
            }`}
          >
            <span aria-hidden className={destination.id === 'record' && recording ? 'text-danger animate-pulse' : ''}>
              {destination.icon}
            </span>
            <span>{destination.label}</span>
          </button>
        ))}
        {pendingOffline > 0 && (
          <p className="text-warn mt-auto px-2 text-xs">{strings.errors.offlinePending(pendingOffline)}</p>
        )}
      </nav>

      <main className="min-h-0 min-w-0 flex-1">
        {route.name === 'record' && (
          <Record onOpenSession={(id) => navigate({ name: 'session', id })} />
        )}
        {route.name === 'history' && (
          <History
            onOpen={(id, utteranceId) => navigate({ name: 'session', id, utteranceId })}
          />
        )}
        {route.name === 'session' && (
          <SessionDetail
            sessionId={route.id}
            focusUtteranceId={route.utteranceId}
            onBack={() => navigate({ name: 'history' })}
          />
        )}
        {route.name === 'settings' && <Settings />}
      </main>

      {/* Phone tab bar, inside thumb reach (FR-UI-5, FR-UI-12) */}
      <nav className="border-line bg-surface-1 flex shrink-0 border-t pb-[env(safe-area-inset-bottom)] sm:hidden">
        {destinations.map((destination) => (
          <button
            key={destination.id}
            type="button"
            onClick={() => navigate({ name: destination.id } as Route)}
            aria-current={active === destination.id ? 'page' : undefined}
            className={`flex flex-1 flex-col items-center gap-0.5 py-2 text-xs transition ${
              active === destination.id ? 'text-fg' : 'text-fg-dim'
            }`}
          >
            <span
              aria-hidden
              className={`text-base ${destination.id === 'record' && recording ? 'text-danger animate-pulse' : ''}`}
            >
              {destination.icon}
            </span>
            {destination.label}
          </button>
        ))}
      </nav>
    </div>
  )
}
