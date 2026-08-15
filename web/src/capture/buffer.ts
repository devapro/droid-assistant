/**
 * Client-side chunk buffer with IndexedDB spill (FR-CAP-6, FR-CAP-15, NFR-RES-5).
 *
 * This is what makes a browser trustworthy for a sixty-minute meeting. Chunks
 * are held until the server acknowledges them; anything unacknowledged survives
 * a dropped connection and is retransmitted from the first gap.
 *
 * Two tiers, because both failure modes are real:
 *   * an in-memory ring covers the common case — a few seconds of jitter — with
 *     no storage latency at all;
 *   * IndexedDB covers a tunnel that is down for minutes, and survives the tab
 *     being reloaded, which memory does not.
 *
 * Everything for a session is deleted when it ends or is abandoned (FR-CAP-15):
 * leaving hours of raw audio in browser storage is a storage-correctness bug
 * before it is anything else.
 */

const DB_NAME = 'droid-assistant'
const DB_VERSION = 1
const STORE = 'chunks'
const SESSION_STORE = 'sessions'

/** Spill to IndexedDB beyond this much unacknowledged audio (SRS §5.3). */
const MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 // ~2 min at 16 kHz mono int16

export interface Chunk {
  seq: number
  tMs: number
  pcm: ArrayBuffer
}

export interface PendingSession {
  sessionId: string
  createdAt: number
  /** Set for a session recorded while the server was unreachable (FR-CAP-17). */
  offline: boolean
  meta: Record<string, unknown>
}

let dbPromise: Promise<IDBDatabase> | null = null

function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE)) {
        // Composite key so a chunk is addressable, and a range query can pull
        // one session's chunks in sequence order.
        db.createObjectStore(STORE, { keyPath: ['sessionId', 'seq'] })
      }
      if (!db.objectStoreNames.contains(SESSION_STORE)) {
        db.createObjectStore(SESSION_STORE, { keyPath: 'sessionId' })
      }
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
  return dbPromise
}

function tx<T>(store: string, mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  return openDb().then(
    (db) =>
      new Promise<T>((resolve, reject) => {
        const transaction = db.transaction(store, mode)
        const request = run(transaction.objectStore(store))
        request.onsuccess = () => resolve(request.result)
        request.onerror = () => reject(request.error)
      }),
  )
}

export class ChunkBuffer {
  private memory = new Map<number, Chunk>()
  private memoryBytes = 0
  private spilled = new Set<number>()
  private acked = -1

  constructor(
    readonly sessionId: string,
    private readonly capBytes: number = 256 * 1024 * 1024,
  ) {}

  get pendingCount(): number {
    return this.memory.size + this.spilled.size
  }

  get pendingBytes(): number {
    return this.memoryBytes
  }

  get ackedThrough(): number {
    return this.acked
  }

  /** Hold a chunk until the server acknowledges it. */
  async add(chunk: Chunk): Promise<void> {
    this.memory.set(chunk.seq, chunk)
    this.memoryBytes += chunk.pcm.byteLength

    if (this.memoryBytes > MEMORY_LIMIT_BYTES) {
      await this.spillOldest()
    }
  }

  private async spillOldest(): Promise<void> {
    const ordered = [...this.memory.keys()].sort((a, b) => a - b)
    for (const seq of ordered) {
      if (this.memoryBytes <= MEMORY_LIMIT_BYTES / 2) break
      const chunk = this.memory.get(seq)
      if (!chunk) continue
      try {
        await tx(STORE, 'readwrite', (store) =>
          store.put({ sessionId: this.sessionId, seq, tMs: chunk.tMs, pcm: chunk.pcm }),
        )
        this.spilled.add(seq)
        this.memory.delete(seq)
        this.memoryBytes -= chunk.pcm.byteLength
      } catch {
        // Quota exhausted or private-browsing storage refused. Keeping the
        // chunk in memory is the lesser evil; the cap below is the backstop.
        break
      }
    }
    if (this.memoryBytes > this.capBytes) {
      // Past the configured cap there is no honest option left. Dropping the
      // oldest is the same choice the server's ring buffer makes, and the count
      // is surfaced rather than swallowed (NFR-RES-5).
      const ordered2 = [...this.memory.keys()].sort((a, b) => a - b)
      for (const seq of ordered2) {
        if (this.memoryBytes <= this.capBytes) break
        const chunk = this.memory.get(seq)
        if (!chunk) continue
        this.memory.delete(seq)
        this.memoryBytes -= chunk.pcm.byteLength
        this.dropped++
      }
    }
  }

  dropped = 0

  /** Release everything the server has confirmed it holds (FR-CAP-15). */
  async ack(throughSeq: number): Promise<void> {
    if (throughSeq <= this.acked) return
    const released: number[] = []
    for (const seq of this.memory.keys()) {
      if (seq <= throughSeq) released.push(seq)
    }
    for (const seq of released) {
      const chunk = this.memory.get(seq)
      if (chunk) this.memoryBytes -= chunk.pcm.byteLength
      this.memory.delete(seq)
    }
    const spilledReleased = [...this.spilled].filter((seq) => seq <= throughSeq)
    if (spilledReleased.length) {
      await Promise.all(
        spilledReleased.map((seq) =>
          tx(STORE, 'readwrite', (store) => store.delete([this.sessionId, seq])).catch(() => undefined),
        ),
      )
      spilledReleased.forEach((seq) => this.spilled.delete(seq))
    }
    this.acked = throughSeq
  }

  /**
   * Everything above `fromSeq`, in order — the retransmit set after a reconnect.
   *
   * Reading from the *first gap* rather than from the newest chunk is what makes
   * recovery lossless: the server acknowledges the highest contiguous sequence,
   * so anything above it may or may not have arrived, and re-sending it is
   * cheap while losing it is not.
   */
  async since(fromSeq: number): Promise<Chunk[]> {
    const out: Chunk[] = []
    for (const [seq, chunk] of this.memory) {
      if (seq >= fromSeq) out.push(chunk)
    }
    if (this.spilled.size) {
      const stored = await this.readSpilled(fromSeq)
      out.push(...stored)
    }
    return out.sort((a, b) => a.seq - b.seq)
  }

  private async readSpilled(fromSeq: number): Promise<Chunk[]> {
    try {
      const db = await openDb()
      return await new Promise<Chunk[]>((resolve, reject) => {
        const store = db.transaction(STORE, 'readonly').objectStore(STORE)
        const range = IDBKeyRange.bound([this.sessionId, fromSeq], [this.sessionId, Infinity])
        const request = store.getAll(range)
        request.onsuccess = () => resolve(request.result as Chunk[])
        request.onerror = () => reject(request.error)
      })
    } catch {
      return []
    }
  }

  /** FR-CAP-15: after a session ends, none of its audio remains in the browser. */
  async clear(): Promise<void> {
    this.memory.clear()
    this.memoryBytes = 0
    this.spilled.clear()
    await clearSession(this.sessionId)
  }
}

export async function clearSession(sessionId: string): Promise<void> {
  try {
    const db = await openDb()
    await new Promise<void>((resolve) => {
      const transaction = db.transaction([STORE, SESSION_STORE], 'readwrite')
      const range = IDBKeyRange.bound([sessionId, -Infinity], [sessionId, Infinity])
      transaction.objectStore(STORE).delete(range)
      transaction.objectStore(SESSION_STORE).delete(sessionId)
      transaction.oncomplete = () => resolve()
      transaction.onerror = () => resolve()
    })
  } catch {
    // Nothing to clean up, or storage is unavailable. Either way, not an error
    // the user can act on.
  }
}

// --- offline sessions (FR-CAP-17) -------------------------------------------

export async function recordPendingSession(entry: PendingSession): Promise<void> {
  await tx(SESSION_STORE, 'readwrite', (store) => store.put(entry)).catch(() => undefined)
}

export async function listPendingSessions(): Promise<PendingSession[]> {
  try {
    return await tx<PendingSession[]>(SESSION_STORE, 'readonly', (store) => store.getAll())
  } catch {
    return []
  }
}

export async function readAllChunks(sessionId: string): Promise<Chunk[]> {
  try {
    const db = await openDb()
    return await new Promise<Chunk[]>((resolve, reject) => {
      const store = db.transaction(STORE, 'readonly').objectStore(STORE)
      const range = IDBKeyRange.bound([sessionId, -Infinity], [sessionId, Infinity])
      const request = store.getAll(range)
      request.onsuccess = () => resolve((request.result as Chunk[]).sort((a, b) => a.seq - b.seq))
      request.onerror = () => reject(request.error)
    })
  } catch {
    return []
  }
}

/** Persist every chunk — used only while recording with no server (FR-CAP-17). */
export async function storeOffline(sessionId: string, chunk: Chunk): Promise<boolean> {
  try {
    await tx(STORE, 'readwrite', (store) =>
      store.put({ sessionId, seq: chunk.seq, tMs: chunk.tMs, pcm: chunk.pcm }),
    )
    return true
  } catch {
    return false
  }
}

export async function estimateQuota(): Promise<{ usage: number; quota: number } | null> {
  if (!navigator.storage?.estimate) return null
  const { usage = 0, quota = 0 } = await navigator.storage.estimate()
  return { usage, quota }
}
