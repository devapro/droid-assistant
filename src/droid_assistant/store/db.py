"""SQLite connection management and migrations.

Everything the server writes goes through one connection pool guarded by an
asyncio lock, and every write runs in a transaction (NFR-REL-5). SQLite in WAL
mode survives an abrupt kill by design; what it does not survive is a half-
written multi-row change outside a transaction, so there are none.

Blocking `sqlite3` calls are pushed to a thread so a slow write cannot stall the
audio path. That is why reads take a connection *per thread* rather than sharing
one: see `Database._reader`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

import numpy as np

from ..domain import Embedding

T = TypeVar("T")

SCHEMA_VERSION = 3


def encode_embedding(vec: Embedding | None) -> bytes | None:
    """float32 little-endian, contiguous — the layout `sqlite-vec` expects, so
    the v2 vector index can be built over these columns without a rewrite."""
    if vec is None:
        return None
    return np.ascontiguousarray(vec, dtype="<f4").tobytes()


def decode_embedding(blob: bytes | None) -> Embedding | None:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype="<f4").copy()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


class Database:
    """An async facade over a single SQLite file.

    Reads run on a read connection per thread; writes are serialised through one
    write connection, which is how SQLite wants to be used and removes the
    `database is locked` class of bug entirely.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()
        self._write: sqlite3.Connection | None = None
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        #: Bumped on close, so a reader cached by a thread that outlives this
        #: `Database` is never handed back after being closed underneath it.
        self._generation = 0

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write = await asyncio.to_thread(self._open)
        await asyncio.to_thread(self._migrate, self._write)

    def _open(self, readonly_hint: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,  # explicit transactions only
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            PRAGMA foreign_keys = ON;
            PRAGMA busy_timeout = 30000;
            PRAGMA temp_store = MEMORY;
            PRAGMA mmap_size = 268435456;
            """
        )
        if readonly_hint:
            conn.execute("PRAGMA query_only = ON")
        return conn

    async def close(self) -> None:
        with self._readers_lock:
            readers, self._readers = self._readers, []
            self._generation += 1
        for conn in (*readers, self._write):
            if conn is not None:
                await asyncio.to_thread(conn.close)
        self._write = None

    # --- migrations ---------------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        current: int = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} was written by a newer droid-assistant "
                f"(schema v{current}, this build understands v{SCHEMA_VERSION}). "
                "Upgrade the server rather than downgrading the database."
            )
        sql = resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
        # `executescript` commits any pending transaction before it runs, so the
        # schema cannot be wrapped in one. Every statement in it is `IF NOT
        # EXISTS`, which makes re-running it safe and makes that acceptable.
        conn.executescript(sql)

        # Migrations for databases created by an earlier version. `schema.sql`
        # is all `IF NOT EXISTS`, so it creates new tables but never alters an
        # existing one; that is what these are for.
        if current < 2:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "cost_breakdown" not in columns:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN cost_breakdown TEXT NOT NULL DEFAULT '{}'"
                )

        # v3 adds `prompts` and nothing else. A brand-new table needs no
        # migration of its own — `schema.sql` above has already created it — so
        # the version bump exists to stop an older build opening a database it
        # would not understand, not to run any statement here.

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # --- reads --------------------------------------------------------------

    def _reader(self) -> sqlite3.Connection:
        """This thread's read connection, opened on first use.

        One connection per thread, not one shared by all of them. `sqlite3` will
        *let* a connection be used from several threads once
        `check_same_thread=False` is set, but it does not serialise the prepared
        statements it caches on that connection: two threads running the same
        SQL get handed the same statement, and bind and step it over each other.
        Reads then fail with `InterfaceError: bad parameter or other API misuse`
        or — worse, because nothing raises — come back with columns belonging to
        another thread's row, or `None`. A `get_session` that answers `None` for
        a session that plainly exists is a 404 on a recording someone is
        watching, so this is not a tidiness fix.

        Since every read is dispatched with `asyncio.to_thread`, the count is
        bounded by the event loop's executor, and WAL means readers never block
        each other or the writer.
        """
        generation = self._generation
        cached = getattr(self._local, "reader", None)
        if cached is not None and cached[0] == generation:
            conn: sqlite3.Connection = cached[1]
            return conn

        conn = self._open(readonly_hint=True)
        with self._readers_lock:
            if self._generation != generation:  # closed while we were opening
                conn.close()
                raise RuntimeError(f"{self.path} is closed")
            self._readers.append(conn)
        self._local.reader = (generation, conn)
        return conn

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        def run() -> sqlite3.Row | None:
            row: sqlite3.Row | None = self._reader().execute(sql, params).fetchone()
            return row

        return await asyncio.to_thread(run)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def run() -> list[sqlite3.Row]:
            return self._reader().execute(sql, params).fetchall()

        return await asyncio.to_thread(run)

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        row = await self.fetch_one(sql, params)
        return default if row is None else row[0]

    # --- writes -------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        await self.transaction(lambda conn: conn.execute(sql, params))

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        batch = list(rows)
        if not batch:
            return
        await self.transaction(lambda conn: conn.executemany(sql, batch))

    async def transaction(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run `work` inside BEGIN IMMEDIATE / COMMIT.

        `work` is synchronous and receives the write connection, so a multi-row
        change — an utterance plus its speaker plus its event-log row — commits
        or vanishes as one (NFR-REL-5).
        """

        def run() -> T:
            assert self._write is not None
            conn = self._write
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = work(conn)
                conn.execute("COMMIT")
                return result
            except Exception:
                conn.execute("ROLLBACK")
                raise

        async with self._write_lock:
            return await asyncio.to_thread(run)

    async def checkpoint(self) -> None:
        """Fold the WAL back into the main file — used before a backup hint."""
        await self.transaction(lambda conn: conn.execute("PRAGMA wal_checkpoint(TRUNCATE)"))
