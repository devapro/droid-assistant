"""SQLite connection management and migrations.

Everything the server writes goes through one connection pool guarded by an
asyncio lock, and every write runs in a transaction (NFR-REL-5). SQLite in WAL
mode survives an abrupt kill by design; what it does not survive is a half-
written multi-row change outside a transaction, so there are none.

Blocking `sqlite3` calls are pushed to a thread so a slow write cannot stall the
audio path.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
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

    Reads run on a shared read connection; writes are serialised through one
    write connection, which is how SQLite wants to be used and removes the
    `database is locked` class of bug entirely.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()
        self._write: sqlite3.Connection | None = None
        self._read: sqlite3.Connection | None = None

    # --- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write = await asyncio.to_thread(self._open)
        self._read = await asyncio.to_thread(self._open, readonly_hint=True)
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
        for conn in (self._read, self._write):
            if conn is not None:
                await asyncio.to_thread(conn.close)
        self._read = self._write = None

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

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        def run() -> sqlite3.Row | None:
            assert self._read is not None
            row: sqlite3.Row | None = self._read.execute(sql, params).fetchone()
            return row

        return await asyncio.to_thread(run)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def run() -> list[sqlite3.Row]:
            assert self._read is not None
            return self._read.execute(sql, params).fetchall()

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
