"""Row ↔ domain mapping. The only module that knows the column names.

Named `repository` rather than the plan's `models`, because "model" already
means an ASR checkpoint everywhere else in this codebase.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..domain import (
    Artifact,
    Embedding,
    LatencyMode,
    SessionState,
    Speaker,
    Utterance,
    Word,
    new_id,
    now_ms,
)
from .db import Database, decode_embedding, dumps, encode_embedding, loads


@dataclass(slots=True)
class SessionRecord:
    id: str
    started_at: int
    state: SessionState = SessionState.RECORDING
    title: str | None = None
    ended_at: int | None = None
    mode: LatencyMode = LatencyMode.BALANCED
    source_languages: list[str] = field(default_factory=list)
    target_language: str = "en"
    vocabulary: list[str] = field(default_factory=list)
    client_user_agent: str | None = None
    mic_label: str | None = None
    audio_constraints: dict[str, Any] = field(default_factory=dict)
    cloud_used: bool = False
    providers_used: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    #: Estimated spend per component — {"asr": …, "translation": …} — so a
    #: session can say *what* the money went on, not only how much.
    cost_breakdown: dict[str, float] = field(default_factory=dict)
    local_only: bool = False
    audio_path: str | None = None
    audio_duration_ms: int | None = None
    tags: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    preset_id: str | None = None
    plugins: list[str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    dropped_chunks: int = 0

    @property
    def duration_ms(self) -> int:
        return (self.ended_at or now_ms()) - self.started_at

    def to_json(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "state": str(self.state),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "mode": str(self.mode),
            "source_languages": self.source_languages,
            "target_language": self.target_language,
            "vocabulary": self.vocabulary,
            "client_user_agent": self.client_user_agent,
            "mic_label": self.mic_label,
            "audio_constraints": self.audio_constraints,
            "cloud_used": self.cloud_used,
            "providers_used": self.providers_used,
            "cost_usd": round(self.cost_usd, 6),
            "cost_breakdown": {k: round(v, 6) for k, v in self.cost_breakdown.items()},
            "local_only": self.local_only,
            "has_audio": bool(self.audio_path),
            "audio_duration_ms": self.audio_duration_ms,
            "tags": self.tags,
            "participants": self.participants,
            "preset_id": self.preset_id,
            "plugins": self.plugins,
            "metadata": self.metadata,
            "dropped_chunks": self.dropped_chunks,
        }
        if extra:
            data.update(extra)
        return data


def _session_from_row(row: sqlite3.Row) -> SessionRecord:
    return SessionRecord(
        id=row["id"],
        title=row["title"],
        state=SessionState(row["state"]),
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        mode=LatencyMode(row["mode"]),
        source_languages=loads(row["source_languages"], []),
        target_language=row["target_language"],
        vocabulary=loads(row["vocabulary"], []),
        client_user_agent=row["client_user_agent"],
        mic_label=row["mic_label"],
        audio_constraints=loads(row["audio_constraints"], {}),
        cloud_used=bool(row["cloud_used"]),
        providers_used=loads(row["providers_used"], []),
        cost_usd=row["cost_usd"],
        cost_breakdown=loads(row["cost_breakdown"], {}),
        local_only=bool(row["local_only"]),
        audio_path=row["audio_path"],
        audio_duration_ms=row["audio_duration_ms"],
        tags=loads(row["tags"], []),
        participants=loads(row["participants"], []),
        preset_id=row["preset_id"],
        plugins=loads(row["plugins"], None),
        metadata=loads(row["metadata"], {}),
        dropped_chunks=row["dropped_chunks"],
    )


def _utterance_from_row(row: sqlite3.Row, *, with_embedding: bool = False) -> Utterance:
    keys = row.keys()
    return Utterance(
        id=row["id"],
        session_id=row["session_id"],
        seq=row["seq"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        speaker_id=row["speaker_id"],
        language=row["language"],
        text=row["text"],
        text_original=row["text_original"],
        translation=row["translation"],
        translation_state=row["translation_state"],
        confidence=row["confidence"],
        words=[Word.from_json(w) for w in loads(row["words_json"], [])],
        marked=bool(row["marked"]),
        edited_at=row["edited_at"],
        timings=loads(row["timings_json"], {}),
        embedding=(
            decode_embedding(row["embedding"]) if with_embedding and "embedding" in keys else None
        ),
    )


def _speaker_from_row(row: sqlite3.Row) -> Speaker:
    return Speaker(
        id=row["id"],
        session_id=row["session_id"],
        label=row["label"],
        index=row["idx"],
        display_name=row["display_name"],
        person_id=row["person_id"],
        centroid_embedding=decode_embedding(row["centroid_embedding"]),
    )


def _prompt_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "instructions": row["instructions"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_used_at": row["last_used_at"],
    }


# Columns read for list views; `embedding` is deliberately excluded because it is
# by far the largest column and no list view needs it.
_UTT_COLS = (
    "id, session_id, seq, start_ms, end_ms, speaker_id, language, text, text_original, "
    "translation, translation_state, confidence, words_json, marked, edited_at, timings_json"
)


class Repository:
    """CRUD over the schema, in domain terms."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- sessions -----------------------------------------------------------

    async def create_session(self, record: SessionRecord) -> SessionRecord:
        await self.db.execute(
            """
            INSERT INTO sessions (
                id, title, state, started_at, ended_at, mode, source_languages,
                target_language, vocabulary, client_user_agent, mic_label,
                audio_constraints, cloud_used, providers_used, cost_usd, cost_breakdown,
                local_only, audio_path, audio_duration_ms, tags, participants, preset_id,
                plugins, metadata, dropped_chunks, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record.id,
                record.title,
                str(record.state),
                record.started_at,
                record.ended_at,
                str(record.mode),
                dumps(record.source_languages),
                record.target_language,
                dumps(record.vocabulary),
                record.client_user_agent,
                record.mic_label,
                dumps(record.audio_constraints),
                int(record.cloud_used),
                dumps(record.providers_used),
                record.cost_usd,
                dumps(record.cost_breakdown),
                int(record.local_only),
                record.audio_path,
                record.audio_duration_ms,
                dumps(record.tags),
                dumps(record.participants),
                record.preset_id,
                dumps(record.plugins) if record.plugins is not None else None,
                dumps(record.metadata),
                record.dropped_chunks,
                now_ms(),
            ),
        )
        return record

    async def get_session(self, session_id: str) -> SessionRecord | None:
        row = await self.db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _session_from_row(row) if row else None

    @staticmethod
    def _session_filters(
        *,
        language: str | None = None,
        tag: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        query: str | None = None,
    ) -> tuple[str, list[Any]]:
        """The WHERE clause shared by listing and counting.

        Shared rather than duplicated because the count is what the client
        paginates against: a total computed over different rows than the page
        means "Load more" either stops early or never stops.
        """
        where: list[str] = []
        params: list[Any] = []
        if language:
            # source_languages is a JSON array; target_language is scalar.
            where.append(
                "(target_language = ? OR EXISTS (SELECT 1 FROM json_each(sessions.source_languages)"
                " WHERE json_each.value = ?))"
            )
            params += [language, language]
        if tag:
            where.append(
                "EXISTS (SELECT 1 FROM json_each(sessions.tags) WHERE json_each.value = ?)"
            )
            params.append(tag)
        if since_ms is not None:
            where.append("started_at >= ?")
            params.append(since_ms)
        if until_ms is not None:
            where.append("started_at <= ?")
            params.append(until_ms)
        if query:
            where.append("title LIKE ?")
            params.append(f"%{query}%")
        return (f"WHERE {' AND '.join(where)}" if where else ""), params

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        language: str | None = None,
        tag: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        query: str | None = None,
    ) -> list[SessionRecord]:
        clause, params = self._session_filters(
            language=language, tag=tag, since_ms=since_ms, until_ms=until_ms, query=query
        )
        rows = await self.db.fetch_all(
            # `started_at DESC, id DESC` — a stable order. Two sessions can share
            # a millisecond, and with only `started_at` SQLite is free to order
            # them differently between two queries, which across a page boundary
            # shows one row twice and hides another entirely.
            f"SELECT * FROM sessions {clause} ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return [_session_from_row(r) for r in rows]

    async def count_sessions(
        self,
        *,
        language: str | None = None,
        tag: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
        query: str | None = None,
    ) -> int:
        """How many sessions match — the same filters the listing applies."""
        clause, params = self._session_filters(
            language=language, tag=tag, since_ms=since_ms, until_ms=until_ms, query=query
        )
        return int(
            await self.db.fetch_value(f"SELECT count(*) FROM sessions {clause}", params, default=0)
        )

    async def session_facets(self) -> dict[str, list[str]]:
        """Every language and tag in use, across all sessions.

        Deliberately unfiltered and independent of the current page. The client
        used to build these lists from whatever rows it happened to have loaded,
        which meant a tag only used on an old session was not offered until you
        had scrolled far enough to load it — the filter could not reach the
        thing it existed to find.
        """
        languages = await self.db.fetch_all(
            "SELECT DISTINCT value AS code FROM sessions, json_each(sessions.source_languages)"
            " WHERE value <> ''"
            " UNION SELECT DISTINCT target_language FROM sessions WHERE target_language <> ''"
        )
        tags = await self.db.fetch_all(
            "SELECT DISTINCT value AS name FROM sessions, json_each(sessions.tags)"
            " WHERE value <> ''"
        )
        return {
            "languages": sorted(str(r[0]) for r in languages),
            "tags": sorted(str(r[0]) for r in tags),
        }

    async def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        json_cols = {
            "cost_breakdown",
            "source_languages",
            "vocabulary",
            "audio_constraints",
            "providers_used",
            "tags",
            "participants",
            "metadata",
            "plugins",
        }
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            if key in json_cols:
                params.append(dumps(value) if value is not None else None)
            elif isinstance(value, bool):
                params.append(int(value))
            elif isinstance(value, SessionState | LatencyMode):
                params.append(str(value))
            else:
                params.append(value)
        params.append(session_id)
        await self.db.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)

    async def add_session_cost(
        self, session_id: str, delta_usd: float, provider: str, component: str = "llm"
    ) -> tuple[float, dict[str, float]]:
        """Accumulate spend, atomically, and return `(total, breakdown)`.

        Read-modify-write inside one transaction: recognition and translation
        bill concurrently, and two `UPDATE … SET cost = cost + ?` racing would
        lose one of them (FR-CFG-7, NFR-REL-5).
        """

        def work(conn: sqlite3.Connection) -> tuple[float, dict[str, float]]:
            row = conn.execute(
                "SELECT cost_usd, cost_breakdown, providers_used FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return 0.0, {}
            total = float(row["cost_usd"]) + delta_usd
            breakdown: dict[str, float] = loads(row["cost_breakdown"], {})
            breakdown[component] = breakdown.get(component, 0.0) + delta_usd
            providers = loads(row["providers_used"], [])
            if provider and provider not in providers:
                providers.append(provider)
            conn.execute(
                "UPDATE sessions SET cost_usd = ?, cost_breakdown = ?, providers_used = ? "
                "WHERE id = ?",
                (total, dumps(breakdown), dumps(providers), session_id),
            )
            return total, breakdown

        return await self.db.transaction(work)

    async def delete_session(self, session_id: str) -> None:
        """FR-SES-12 — cascades to utterances, speakers, artifacts, tokens, events."""
        await self.db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    async def recover_orphaned_sessions(self) -> list[str]:
        """Finalise sessions left mid-recording by a crash (FR-SES-3).

        Their utterances were committed as they arrived, so the transcript is
        whatever reached the database before the kill; the session just needs an
        end time. `ended_at` is set from the last utterance rather than now, so a
        session interrupted yesterday does not claim to have run overnight.
        """

        def work(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT id, started_at FROM sessions WHERE state IN ('recording', 'processing')"
            ).fetchall()
            ids: list[str] = []
            for row in rows:
                last = conn.execute(
                    "SELECT max(start_ms + (end_ms - start_ms)) FROM utterances "
                    "WHERE session_id = ?",
                    (row["id"],),
                ).fetchone()[0]
                ended = row["started_at"] + int(last or 0)
                conn.execute(
                    "UPDATE sessions SET state = 'ended', ended_at = ?, "
                    "metadata = json_set(metadata, '$.recovered', json('true')) WHERE id = ?",
                    (ended, row["id"]),
                )
                ids.append(row["id"])
            return ids

        return await self.db.transaction(work)

    # --- utterances ---------------------------------------------------------

    async def next_utterance_seq(self, session_id: str) -> int:
        value = await self.db.fetch_value(
            "SELECT coalesce(max(seq), -1) + 1 FROM utterances WHERE session_id = ?",
            (session_id,),
            default=0,
        )
        return int(value)

    async def add_utterance(self, utt: Utterance) -> None:
        await self.db.execute(
            f"""
            INSERT INTO utterances (
                {_UTT_COLS}, embedding, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT (id) DO UPDATE SET
                start_ms = excluded.start_ms,
                end_ms = excluded.end_ms,
                speaker_id = excluded.speaker_id,
                language = excluded.language,
                text = excluded.text,
                translation = excluded.translation,
                translation_state = excluded.translation_state,
                confidence = excluded.confidence,
                words_json = excluded.words_json,
                marked = excluded.marked,
                timings_json = excluded.timings_json,
                embedding = coalesce(excluded.embedding, utterances.embedding)
            """,
            (
                utt.id,
                utt.session_id,
                utt.seq,
                utt.start_ms,
                utt.end_ms,
                utt.speaker_id,
                utt.language,
                utt.text,
                utt.text_original,
                utt.translation,
                utt.translation_state,
                utt.confidence,
                dumps([w.to_json() for w in utt.words]) if utt.words else None,
                int(utt.marked),
                utt.edited_at,
                dumps(utt.timings) if utt.timings else None,
                encode_embedding(utt.embedding),
                now_ms(),
            ),
        )

    async def get_utterance(self, utterance_id: str) -> Utterance | None:
        row = await self.db.fetch_one(
            f"SELECT {_UTT_COLS} FROM utterances WHERE id = ?", (utterance_id,)
        )
        return _utterance_from_row(row) if row else None

    async def list_utterances(
        self, session_id: str, *, limit: int | None = None, offset: int = 0
    ) -> list[Utterance]:
        sql = f"SELECT {_UTT_COLS} FROM utterances WHERE session_id = ? ORDER BY seq"
        params: list[Any] = [session_id]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        rows = await self.db.fetch_all(sql, params)
        return [_utterance_from_row(r) for r in rows]

    async def update_utterance(self, utterance_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(
                int(value)
                if isinstance(value, bool)
                else encode_embedding(value)
                if key == "embedding"
                else value
            )
        params.append(utterance_id)
        await self.db.execute(f"UPDATE utterances SET {', '.join(sets)} WHERE id = ?", params)

    async def edit_utterance_text(self, utterance_id: str, text: str) -> Utterance | None:
        """FR-SES-8: mark as edited and keep the original retrievable.

        `text_original` is written only on the first edit, so a second correction
        does not overwrite the machine's original output with the first human one.
        """

        def work(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE utterances SET text_original = coalesce(text_original, text), "
                "text = ?, edited_at = ? WHERE id = ?",
                (text, now_ms(), utterance_id),
            )

        await self.db.transaction(work)
        return await self.get_utterance(utterance_id)

    async def utterance_embeddings(self, session_id: str) -> list[tuple[str, Embedding | None]]:
        rows = await self.db.fetch_all(
            "SELECT id, embedding FROM utterances WHERE session_id = ? ORDER BY seq", (session_id,)
        )
        return [(r["id"], decode_embedding(r["embedding"])) for r in rows]

    # --- speakers -----------------------------------------------------------

    async def ensure_speaker(self, session_id: str, index: int) -> Speaker:
        """Get or create the speaker for a diarizer index, atomically."""

        def work(conn: sqlite3.Connection) -> sqlite3.Row:
            row: sqlite3.Row | None = conn.execute(
                "SELECT * FROM speakers WHERE session_id = ? AND idx = ?", (session_id, index)
            ).fetchone()
            if row is not None:
                return row
            speaker_id = new_id("spk")
            conn.execute(
                "INSERT INTO speakers (id, session_id, label, idx) VALUES (?,?,?,?)",
                (speaker_id, session_id, f"Speaker {index + 1}", index),
            )
            created: sqlite3.Row = conn.execute(
                "SELECT * FROM speakers WHERE id = ?", (speaker_id,)
            ).fetchone()
            return created

        return _speaker_from_row(await self.db.transaction(work))

    async def list_speakers(self, session_id: str) -> list[Speaker]:
        rows = await self.db.fetch_all(
            "SELECT * FROM speakers WHERE session_id = ? ORDER BY idx", (session_id,)
        )
        return [_speaker_from_row(r) for r in rows]

    async def rename_speaker(self, speaker_id: str, display_name: str | None) -> Speaker | None:
        """FR-DIA-4. One row changes; every utterance references it, so the
        rename is retroactive across the whole session by construction."""
        await self.db.execute(
            "UPDATE speakers SET display_name = ? WHERE id = ?", (display_name or None, speaker_id)
        )
        row = await self.db.fetch_one("SELECT * FROM speakers WHERE id = ?", (speaker_id,))
        return _speaker_from_row(row) if row else None

    async def set_speaker_centroid(self, speaker_id: str, centroid: Embedding) -> None:
        await self.db.execute(
            "UPDATE speakers SET centroid_embedding = ? WHERE id = ?",
            (encode_embedding(centroid), speaker_id),
        )

    # --- artifacts ----------------------------------------------------------

    async def add_artifact(
        self, session_id: str, plugin_name: str, plugin_version: str, artifact: Artifact
    ) -> dict[str, Any]:
        """Insert as a new version, superseding the previous one (FR-PLG-12)."""

        def work(conn: sqlite3.Connection) -> dict[str, Any]:
            prev = conn.execute(
                "SELECT id, version FROM artifacts WHERE session_id = ? AND plugin_name = ? "
                "AND kind = ? ORDER BY version DESC LIMIT 1",
                (session_id, plugin_name, artifact.kind),
            ).fetchone()
            version = (prev["version"] + 1) if prev else 1
            artifact_id = new_id("art")
            conn.execute(
                "INSERT INTO artifacts (id, session_id, plugin_name, plugin_version, kind, mime, "
                "content, version, metadata, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    session_id,
                    plugin_name,
                    plugin_version,
                    artifact.kind,
                    artifact.mime,
                    artifact.content,
                    version,
                    dumps(artifact.metadata),
                    now_ms(),
                ),
            )
            if prev:
                conn.execute(
                    "UPDATE artifacts SET superseded_by = ? WHERE id = ?", (artifact_id, prev["id"])
                )
            return {
                "id": artifact_id,
                "session_id": session_id,
                "plugin_name": plugin_name,
                "plugin_version": plugin_version,
                "kind": artifact.kind,
                "mime": artifact.mime,
                "content": artifact.content,
                "version": version,
                "current": True,
                "metadata": artifact.metadata,
            }

        return await self.db.transaction(work)

    async def list_artifacts(
        self, session_id: str, *, current_only: bool = False
    ) -> list[dict[str, Any]]:
        clause = " AND superseded_by IS NULL" if current_only else ""
        rows = await self.db.fetch_all(
            f"SELECT * FROM artifacts WHERE session_id = ?{clause} "
            "ORDER BY plugin_name, kind, version DESC",
            (session_id,),
        )
        return [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "plugin_name": r["plugin_name"],
                "plugin_version": r["plugin_version"],
                "kind": r["kind"],
                "mime": r["mime"],
                "content": r["content"],
                "version": r["version"],
                "current": r["superseded_by"] is None,
                "created_at": r["created_at"],
                "metadata": loads(r["metadata"], {}),
            }
            for r in rows
        ]

    async def artifacts_stale(self, session_id: str) -> bool:
        """True when a transcript edit is at least as recent as the newest
        artifact (FR-SES-9).

        `>=` rather than `>` because both stamps have millisecond resolution and
        an edit can land in the same millisecond as the run it invalidates. The
        comparison is deliberately conservative: a spurious offer to re-run
        costs one optional click, while a missed one leaves a summary silently
        describing text that has since changed.
        """
        newest_edit = await self.db.fetch_value(
            "SELECT max(edited_at) FROM utterances WHERE session_id = ?", (session_id,)
        )
        newest_artifact = await self.db.fetch_value(
            "SELECT max(created_at) FROM artifacts WHERE session_id = ? AND superseded_by IS NULL",
            (session_id,),
        )
        return bool(newest_edit and newest_artifact and newest_edit >= newest_artifact)

    # --- plugin state -------------------------------------------------------

    async def plugin_states(self) -> dict[str, dict[str, Any]]:
        rows = await self.db.fetch_all("SELECT * FROM plugin_state")
        return {
            r["plugin_name"]: {
                "enabled": bool(r["enabled"]),
                "config": loads(r["config_json"], {}),
                "last_error": r["last_error"],
                "last_error_at": r["last_error_at"],
            }
            for r in rows
        }

    async def set_plugin_state(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
        error: str | None = None,
        clear_error: bool = False,
    ) -> None:
        def work(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO plugin_state (plugin_name) VALUES (?) ON CONFLICT DO NOTHING", (name,)
            )
            if enabled is not None:
                conn.execute(
                    "UPDATE plugin_state SET enabled = ? WHERE plugin_name = ?",
                    (int(enabled), name),
                )
            if config is not None:
                conn.execute(
                    "UPDATE plugin_state SET config_json = ? WHERE plugin_name = ?",
                    (dumps(config), name),
                )
            if error is not None:
                conn.execute(
                    "UPDATE plugin_state SET last_error = ?, last_error_at = ? "
                    "WHERE plugin_name = ?",
                    (error, now_ms(), name),
                )
            elif clear_error:
                conn.execute(
                    "UPDATE plugin_state SET last_error = NULL, last_error_at = NULL "
                    "WHERE plugin_name = ?",
                    (name,),
                )

        await self.db.transaction(work)

    # --- ingest tokens ------------------------------------------------------

    async def issue_token(self, session_id: str, ttl_ms: int) -> str:
        import secrets

        token = secrets.token_urlsafe(32)
        issued = now_ms()
        await self.db.execute(
            "INSERT INTO ingest_tokens (token, session_id, issued_at, expires_at) VALUES (?,?,?,?)",
            (token, session_id, issued, issued + ttl_ms),
        )
        return token

    async def resolve_token(self, token: str) -> str | None:
        """Return the session id for a live token (NFR-SEC-3), else None."""
        row = await self.db.fetch_one(
            "SELECT session_id, expires_at, revoked_at FROM ingest_tokens WHERE token = ?", (token,)
        )
        if row is None or row["revoked_at"] is not None or row["expires_at"] < now_ms():
            return None
        return str(row["session_id"])

    async def revoke_tokens(self, session_id: str) -> None:
        await self.db.execute(
            "UPDATE ingest_tokens SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
            (now_ms(), session_id),
        )

    # --- event log ----------------------------------------------------------

    async def append_events(self, rows: Sequence[tuple[str, int, str, str]]) -> None:
        """(session_id, seq, type, payload_json) — batched; called off the hot path."""
        await self.db.execute_many(
            "INSERT INTO events_log (session_id, seq, type, payload_json, created_at) "
            f"VALUES (?,?,?,?,{now_ms()})",
            rows,
        )

    async def replay_events(
        self, session_id: str, after_seq: int, limit: int = 2000
    ) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT seq, type, payload_json FROM events_log WHERE session_id = ? AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (session_id, after_seq, limit),
        )
        return [loads(r["payload_json"], {}) for r in rows]

    async def max_event_seq(self, session_id: str) -> int:
        return int(
            await self.db.fetch_value(
                "SELECT coalesce(max(seq), 0) FROM events_log WHERE session_id = ?",
                (session_id,),
                default=0,
            )
        )

    # --- presets (FR-SES-14) ------------------------------------------------

    async def list_presets(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM presets ORDER BY last_used_at DESC NULLS LAST, name"
        )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "config": loads(r["config_json"], {}),
                "created_at": r["created_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]

    async def upsert_preset(
        self, name: str, config: dict[str, Any], preset_id: str | None = None
    ) -> dict[str, Any]:
        pid = preset_id or new_id("pre")
        await self.db.execute(
            "INSERT INTO presets (id, name, config_json, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT (name) DO UPDATE SET config_json = excluded.config_json",
            (pid, name, dumps(config), now_ms()),
        )
        row = await self.db.fetch_one("SELECT * FROM presets WHERE name = ?", (name,))
        assert row is not None
        return {"id": row["id"], "name": row["name"], "config": loads(row["config_json"], {})}

    async def touch_preset(self, preset_id: str) -> None:
        await self.db.execute(
            "UPDATE presets SET last_used_at = ? WHERE id = ?", (now_ms(), preset_id)
        )

    async def delete_preset(self, preset_id: str) -> None:
        await self.db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))

    # --- prompts (FR-PLG-14) ------------------------------------------------

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Most recently used first, as the selector wants it.

        The same ordering serves both places these appear: the picker beside a
        Generate button, where the prompt used last time is nearly always the
        one wanted again, and the editor in Settings, where a stable order
        matters more than an alphabetical one.
        """
        rows = await self.db.fetch_all(
            "SELECT * FROM prompts ORDER BY last_used_at DESC NULLS LAST, name"
        )
        return [_prompt_json(r) for r in rows]

    async def get_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        row = await self.db.fetch_one("SELECT * FROM prompts WHERE id = ?", (prompt_id,))
        return None if row is None else _prompt_json(row)

    async def upsert_prompt(
        self, name: str, instructions: str, prompt_id: str | None = None
    ) -> dict[str, Any]:
        """Create a prompt, or rewrite the one `prompt_id` names.

        An id means "this prompt, whatever it is now called", which is what lets
        the editor rename one. Without an id the name is the identity, so saving
        an existing name edits it rather than failing on the unique constraint or
        filing a second prompt the operator cannot tell apart from the first.
        """
        now = now_ms()
        if prompt_id is not None:
            await self.db.execute(
                "UPDATE prompts SET name = ?, instructions = ?, updated_at = ? WHERE id = ?",
                (name, instructions, now, prompt_id),
            )
            row = await self.db.fetch_one("SELECT * FROM prompts WHERE id = ?", (prompt_id,))
            if row is None:
                raise KeyError(prompt_id)
            return _prompt_json(row)

        await self.db.execute(
            "INSERT INTO prompts (id, name, instructions, created_at, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT (name) DO UPDATE SET "
            "instructions = excluded.instructions, updated_at = excluded.updated_at",
            (new_id("prm"), name, instructions, now, now),
        )
        row = await self.db.fetch_one("SELECT * FROM prompts WHERE name = ?", (name,))
        assert row is not None
        return _prompt_json(row)

    async def touch_prompt(self, prompt_id: str) -> None:
        """Record that a run used it — what the most-recently-used order is for."""
        await self.db.execute(
            "UPDATE prompts SET last_used_at = ? WHERE id = ?", (now_ms(), prompt_id)
        )

    async def delete_prompt(self, prompt_id: str) -> None:
        await self.db.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
