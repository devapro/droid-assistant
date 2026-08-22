"""Storage: transactions, search, audio, and the embedding column (NFR-REL-5).

FR-DIA-5 gets a test here rather than in the diarization suite, because the
requirement is a *storage* one: embeddings must be written from v1 onward, since
adding the column later means never being able to attribute historical sessions.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from droid_assistant.domain import Artifact, Utterance, new_id, now_ms
from droid_assistant.store import Database, Repository, SearchIndex
from droid_assistant.store.db import decode_embedding, encode_embedding
from droid_assistant.store.repository import SessionRecord


async def make_session(repo: Repository, **kwargs) -> SessionRecord:
    record = SessionRecord(id=new_id("sess"), started_at=now_ms(), **kwargs)
    await repo.create_session(record)
    return record


async def add(repo: Repository, session_id: str, seq: int, text: str, **kwargs) -> Utterance:
    utterance = Utterance(
        id=new_id("utt"),
        session_id=session_id,
        seq=seq,
        start_ms=seq * 1000,
        end_ms=seq * 1000 + 900,
        text=text,
        **kwargs,
    )
    await repo.add_utterance(utterance)
    return utterance


class TestTransactions:
    async def test_a_failing_transaction_leaves_nothing_behind(self, db: Database) -> None:
        """NFR-REL-5: a multi-row change commits or vanishes as one."""

        def work(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO sessions (id, state, started_at, created_at) VALUES (?,?,?,?)",
                ("sess_a", "recording", 0, 0),
            )
            raise RuntimeError("interrupted half-way")

        with pytest.raises(RuntimeError):
            await db.transaction(work)
        assert await db.fetch_value("SELECT count(*) FROM sessions", default=0) == 0

    async def test_wal_is_enabled(self, db: Database) -> None:
        """WAL is what makes an abrupt kill survivable rather than corrupting."""
        assert (await db.fetch_value("PRAGMA journal_mode")).lower() == "wal"

    async def test_concurrent_reads_are_not_served_a_shared_statement(
        self, repo: Repository, db: Database
    ) -> None:
        """Reads from several threads at once must all answer truthfully.

        Every read is dispatched with `asyncio.to_thread`, so a session being
        watched while it records has the same row read from many threads at
        once. Sharing one connection between them shares its statement cache
        too, and the same prepared statement bound and stepped from two threads
        returns another row's columns, `None`, or raises `InterfaceError` — a
        404 on a session the reader is looking at. One connection per thread is
        what keeps that from happening.
        """
        record = await make_session(
            repo, client_user_agent="Mozilla/5.0 (a long enough string to notice)"
        )
        for seq in range(40):
            await add(repo, record.id, seq, f"line {seq}")

        async def read() -> None:
            for _ in range(30):
                got = await repo.get_session(record.id)
                assert got is not None, "a session that exists read back as missing"
                assert got.id == record.id
                assert got.state is record.state
                assert got.client_user_agent == record.client_user_agent
                assert len(await repo.list_utterances(record.id)) == 40

        await asyncio.gather(*(read() for _ in range(12)))

    async def test_foreign_keys_cascade_on_delete(self, repo: Repository) -> None:
        """FR-SES-12: deleting a session leaves no residual row."""
        session = await make_session(repo)
        await repo.ensure_speaker(session.id, 0)
        await add(repo, session.id, 0, "hello")
        await repo.add_artifact(session.id, "summary", "1.0", Artifact(kind="summary", content="s"))

        await repo.delete_session(session.id)
        for table in ("utterances", "speakers", "artifacts", "ingest_tokens", "events_log"):
            count = await repo.db.fetch_value(
                f"SELECT count(*) FROM {table} WHERE session_id = ?", (session.id,), default=0
            )
            assert count == 0, table


class TestEmbeddings:
    def test_round_trip_preserves_the_vector(self) -> None:
        vector = np.random.default_rng(0).normal(size=192).astype(np.float32)
        assert np.allclose(decode_embedding(encode_embedding(vector)), vector)

    def test_layout_is_the_one_sqlite_vec_expects(self) -> None:
        """Stored as contiguous little-endian float32 so the v2 vector index can
        be built over these columns without a rewrite."""
        vector = np.arange(4, dtype=np.float32)
        blob = encode_embedding(vector)
        assert blob is not None
        assert len(blob) == 16
        assert np.frombuffer(blob, dtype="<f4").tolist() == [0.0, 1.0, 2.0, 3.0]

    async def test_embeddings_are_persisted_from_v1(self, repo: Repository) -> None:
        """FR-DIA-5. Skipping this is the one decision in M3 that is expensive
        to reverse — a column and a function call now, or no retroactive
        attribution ever."""
        session = await make_session(repo)
        vector = np.random.default_rng(1).normal(size=192).astype(np.float32)
        await add(repo, session.id, 0, "hello", embedding=vector)

        stored = await repo.utterance_embeddings(session.id)
        assert len(stored) == 1
        assert stored[0][1] is not None
        assert np.allclose(stored[0][1], vector)

    async def test_an_update_without_an_embedding_does_not_erase_it(self, repo: Repository) -> None:
        session = await make_session(repo)
        vector = np.ones(8, dtype=np.float32)
        utterance = await add(repo, session.id, 0, "hello", embedding=vector)

        utterance.embedding = None
        utterance.text = "hello again"
        await repo.add_utterance(utterance)

        stored = await repo.utterance_embeddings(session.id)
        assert stored[0][1] is not None


class TestSearch:
    async def test_finds_transcripts_and_artifacts(self, repo: Repository, db: Database) -> None:
        """FR-SES-10: a phrase that appears only in a generated summary returns
        that summary; one that appears only in speech returns the utterance."""
        session = await make_session(repo, title="Standup")
        await add(repo, session.id, 0, "we need to finish the migration by Friday")
        await repo.add_artifact(
            session.id,
            "summary",
            "1.0",
            Artifact(kind="summary", content="The team agreed on a deployment window."),
        )

        index = SearchIndex(db)

        spoken = await index.search("migration")
        assert len(spoken) == 1
        assert spoken[0].kind == "utterance"
        assert "⟦migration⟧" in spoken[0].snippet

        generated = await index.search("deployment window")
        assert generated
        assert generated[0].kind == "artifact"
        assert "⟦" in generated[0].snippet

    async def test_search_covers_translations(self, repo: Repository, db: Database) -> None:
        session = await make_session(repo)
        await add(
            repo,
            session.id,
            0,
            "Нам нужно закончить",
            translation="We need to finish",
            translation_state="done",
        )
        hits = await SearchIndex(db).search("finish")
        assert hits
        assert "⟦finish⟧" in hits[0].snippet

    async def test_diacritics_are_folded(self, repo: Repository, db: Database) -> None:
        """A Latin-script Serbian query must match text written with diacritics."""
        session = await make_session(repo)
        await add(repo, session.id, 0, "Da, do petka, hvala Marković")
        assert await SearchIndex(db).search("Markovic")

    async def test_fts_operators_in_a_query_are_not_syntax(
        self, repo: Repository, db: Database
    ) -> None:
        """A user typing `budget: Q3` means the words, not a column filter."""
        session = await make_session(repo)
        await add(repo, session.id, 0, "the budget for Q3 is approved")
        hits = await SearchIndex(db).search("budget: Q3")
        assert hits

    async def test_empty_query_returns_nothing_rather_than_everything(
        self, repo: Repository, db: Database
    ) -> None:
        session = await make_session(repo)
        await add(repo, session.id, 0, "content")
        assert await SearchIndex(db).search("   ") == []

    async def test_deleting_a_session_removes_it_from_the_index(
        self, repo: Repository, db: Database
    ) -> None:
        session = await make_session(repo)
        await add(repo, session.id, 0, "ephemeral content here")
        index = SearchIndex(db)
        assert await index.search("ephemeral")
        await repo.delete_session(session.id)
        assert await index.search("ephemeral") == []

    async def test_editing_updates_the_index(self, repo: Repository, db: Database) -> None:
        session = await make_session(repo)
        utterance = await add(repo, session.id, 0, "original wording")
        index = SearchIndex(db)
        await repo.edit_utterance_text(utterance.id, "corrected wording")
        assert await index.search("corrected")
        assert await index.search("original") == []


class TestArtifactVersioning:
    async def test_rerunning_supersedes_rather_than_destroys(self, repo: Repository) -> None:
        """FR-PLG-12: after two runs both versions are retrievable and the
        current one is indicated."""
        session = await make_session(repo)
        first = await repo.add_artifact(
            session.id, "summary", "1.0", Artifact(kind="summary", content="v1")
        )
        second = await repo.add_artifact(
            session.id, "summary", "1.0", Artifact(kind="summary", content="v2")
        )

        assert first["version"] == 1
        assert second["version"] == 2

        all_versions = await repo.list_artifacts(session.id)
        assert len(all_versions) == 2
        current = await repo.list_artifacts(session.id, current_only=True)
        assert len(current) == 1
        assert current[0]["content"] == "v2"

    async def test_an_edit_marks_artifacts_stale(self, repo: Repository) -> None:
        """FR-SES-9: after an edit the summary the session holds is out of date,
        and the UI offers to re-run it."""
        session = await make_session(repo)
        utterance = await add(repo, session.id, 0, "original")
        await repo.add_artifact(session.id, "summary", "1.0", Artifact(kind="summary", content="s"))
        assert not await repo.artifacts_stale(session.id)

        await repo.edit_utterance_text(utterance.id, "corrected")
        assert await repo.artifacts_stale(session.id)


class TestTokens:
    async def test_a_valid_token_resolves_to_its_session(self, repo: Repository) -> None:
        session = await make_session(repo)
        token = await repo.issue_token(session.id, 60_000)
        assert await repo.resolve_token(token) == session.id

    async def test_an_expired_token_does_not_resolve(self, repo: Repository) -> None:
        session = await make_session(repo)
        token = await repo.issue_token(session.id, -1)
        assert await repo.resolve_token(token) is None

    async def test_revoked_tokens_do_not_resolve(self, repo: Repository) -> None:
        session = await make_session(repo)
        token = await repo.issue_token(session.id, 60_000)
        await repo.revoke_tokens(session.id)
        assert await repo.resolve_token(token) is None

    async def test_an_unknown_token_does_not_resolve(self, repo: Repository) -> None:
        assert await repo.resolve_token("made-up") is None


class TestAudio:
    def test_range_header_parsing(self) -> None:
        """FR-UI-8 depends on this: without ranges, seeking to minute 30
        re-downloads the whole file."""
        from droid_assistant.store.audio import parse_range

        assert parse_range("bytes=0-99", 1000) == (0, 99)
        assert parse_range("bytes=500-", 1000) == (500, 999)
        assert parse_range("bytes=-100", 1000) == (900, 999)
        assert parse_range("bytes=0-9999", 1000) == (0, 999)  # clamped

    def test_malformed_or_absent_ranges_return_none(self) -> None:
        from droid_assistant.store.audio import parse_range

        for header in (None, "", "items=0-99", "bytes=abc-def", "bytes=2000-3000", "bytes=99-10"):
            assert parse_range(header, 1000) is None, header

    async def test_writer_produces_a_playable_file_of_the_right_duration(
        self, tmp_path: Path
    ) -> None:
        """FR-SIG-3: duration matches the session ± 1 s."""
        from droid_assistant.store.audio import SessionAudioWriter, read_pcm

        writer = SessionAudioWriter("sess_test", tmp_path, now_ms(), codec="wav")
        for _ in range(10):
            await writer.append(np.zeros(16_000, dtype=np.float32))  # 1 s each
        path, duration_ms = await writer.finalize()

        assert path is not None and path.exists()
        assert abs(duration_ms - 10_000) < 1000
        assert read_pcm(path).size == 160_000

    async def test_a_pause_leaves_a_real_gap_in_the_timeline(self, tmp_path: Path) -> None:
        """FR-CAP-16: the timeline reflects the gap, so an utterance offset
        still points at the right moment in the audio."""
        from droid_assistant.store.audio import SessionAudioWriter

        writer = SessionAudioWriter("sess_pause", tmp_path, now_ms(), codec="wav")
        await writer.append(np.ones(16_000, dtype=np.float32) * 0.1)
        await writer.append_silence(5_000)
        await writer.append(np.ones(16_000, dtype=np.float32) * 0.1)
        _path, duration_ms = await writer.finalize()
        assert abs(duration_ms - 7_000) < 100

    async def test_an_empty_session_writes_no_file(self, tmp_path: Path) -> None:
        from droid_assistant.store.audio import SessionAudioWriter

        writer = SessionAudioWriter("sess_empty", tmp_path, now_ms(), codec="wav")
        path, duration = await writer.finalize()
        assert path is None
        assert duration == 0
        assert not list(tmp_path.rglob("sess_empty*"))

    def test_audio_is_laid_out_by_date(self, tmp_path: Path) -> None:
        """SRS §5.9: audio/YYYY/MM/DD/, in UTC so the tree does not reorder
        itself when the operator travels."""
        from droid_assistant.store.audio import session_audio_dir

        # 2026-08-15T00:30Z — late evening on the 14th in several timezones.
        path = session_audio_dir(tmp_path, 1_786_840_200_000)
        assert path.relative_to(tmp_path).as_posix().count("/") == 2


class TestPagination:
    async def test_listing_is_newest_first_and_paged(self, repo: Repository) -> None:
        for i in range(5):
            await make_session(repo, title=f"session {i}")
        page = await repo.list_sessions(limit=2)
        assert len(page) == 2
        assert page[0].started_at >= page[1].started_at
        assert len(await repo.list_sessions(limit=2, offset=4)) == 1
        assert await repo.count_sessions() == 5

    async def test_filtering_by_tag_and_language(self, repo: Repository) -> None:
        await make_session(repo, title="tagged", tags=["infra"], source_languages=["ru"])
        await make_session(repo, title="plain", tags=[], source_languages=["en"])

        assert [s.title for s in await repo.list_sessions(tag="infra")] == ["tagged"]
        assert [s.title for s in await repo.list_sessions(language="ru")] == ["tagged"]


class TestPrompts:
    """FR-PLG-14: the saved instructions a summary can be generated with."""

    async def test_the_one_used_last_is_offered_first(self, repo: Repository) -> None:
        """The picker's whole job is to make the usual choice one tap away, and
        the usual choice is nearly always the previous one."""
        standup = await repo.upsert_prompt("Standup", "terse")
        await repo.upsert_prompt("Customer call", "lead with the ask")

        # Nothing used yet, so alphabetical — an arbitrary order would make the
        # list appear to reshuffle itself between visits.
        assert [p["name"] for p in await repo.list_prompts()] == ["Customer call", "Standup"]

        await repo.touch_prompt(standup["id"])
        assert (await repo.list_prompts())[0]["name"] == "Standup"

    async def test_an_unknown_id_reads_as_absent_rather_than_raising(
        self, repo: Repository
    ) -> None:
        """What the run endpoint turns into a 404."""
        assert await repo.get_prompt("prm_nope") is None

    async def test_rewriting_by_id_keeps_the_row(self, repo: Repository) -> None:
        saved = await repo.upsert_prompt("Standup", "terse")
        rewritten = await repo.upsert_prompt("Daily", "even terser", saved["id"])
        assert rewritten["id"] == saved["id"]
        assert [p["name"] for p in await repo.list_prompts()] == ["Daily"]


class TestSchemaUpgrade:
    async def test_a_database_written_before_prompts_existed_gains_the_table(
        self, data_dir: Path
    ) -> None:
        """v3 adds a table and nothing else, and real databases exist at v2.

        `schema.sql` is all `IF NOT EXISTS`, so opening one creates the table —
        this asserts that rather than trusting it, because the alternative is
        every prompt request failing on a database that has been in use.
        """
        path = data_dir / "upgrade.db"
        first = Database(path)
        await first.connect()
        await first.close()

        # Rewind to a database that predates the table.
        conn = sqlite3.connect(path)
        conn.executescript("DROP TABLE prompts; PRAGMA user_version = 2;")
        conn.close()

        upgraded = Database(path)
        await upgraded.connect()
        try:
            assert await Repository(upgraded).list_prompts() == []
            assert await upgraded.fetch_value("PRAGMA user_version") == 3
        finally:
            await upgraded.close()

    async def test_a_database_from_a_newer_build_is_refused(self, data_dir: Path) -> None:
        """Downgrading the server is not a way to open a database it does not
        understand; upgrading the server is."""
        path = data_dir / "future.db"
        conn = sqlite3.connect(path)
        conn.executescript("PRAGMA user_version = 99;")
        conn.close()

        with pytest.raises(RuntimeError, match="newer droid-assistant"):
            await Database(path).connect()
