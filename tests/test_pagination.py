"""Paging the history list (SRS §5.1).

The list used to fetch a flat 100 sessions and stop, with nothing saying so:
past that, older conversations did not exist as far as the UI was concerned.
These cover the parts that make paging correct rather than merely present — a
stable order across page boundaries, and a total that counts what the filters
match rather than what happens to be in the table.
"""

from __future__ import annotations

from typing import Any

import pytest

from droid_assistant.domain import SessionState
from droid_assistant.store.repository import Repository, SessionRecord


async def _make(repo: Repository, count: int, **fields: Any) -> list[str]:
    ids = []
    for index in range(count):
        record = SessionRecord(
            id=f"sess_{index:04d}",
            # Descending by time, so index 0 is newest and paging order is
            # predictable regardless of how fast the test runs.
            started_at=1_700_000_000_000 - index * 1000,
            state=SessionState.ENDED,
            title=f"Session {index}",
            **fields,
        )
        await repo.create_session(record)
        ids.append(record.id)
    return ids


class TestPaging:
    @pytest.mark.asyncio
    async def test_pages_cover_everything_exactly_once(self, repo: Repository) -> None:
        await _make(repo, 25)
        seen: list[str] = []
        for offset in range(0, 25, 10):
            page = await repo.list_sessions(limit=10, offset=offset)
            seen += [record.id for record in page]
        assert len(seen) == 25
        assert len(set(seen)) == 25, "a row appeared on two pages"

    @pytest.mark.asyncio
    async def test_order_is_stable_when_timestamps_collide(self, repo: Repository) -> None:
        """Two sessions can share a millisecond, and paging must survive it.

        SQLite guarantees no particular order among equal sort keys, so ordering
        by `started_at` alone leaves it free to break the tie differently between
        two queries — across a page boundary that shows one row twice and hides
        another entirely. Hence the `id DESC` tiebreak.

        Honest caveat: with the current plan SQLite happens to be stable anyway,
        so this passes with or without that tiebreak. It guards the property
        against a future index on `started_at` changing the plan; it is not a
        regression test that fails today if the tiebreak is removed.
        """
        for index in range(20):
            await repo.create_session(
                SessionRecord(
                    id=f"sess_{index:04d}",
                    started_at=1_700_000_000_000,  # every one identical
                    state=SessionState.ENDED,
                    title=f"Session {index}",
                )
            )
        first = [r.id for r in await repo.list_sessions(limit=10, offset=0)]
        second = [r.id for r in await repo.list_sessions(limit=10, offset=10)]
        assert set(first) & set(second) == set()
        assert len(set(first) | set(second)) == 20

    @pytest.mark.asyncio
    async def test_offset_past_the_end_is_empty_not_an_error(self, repo: Repository) -> None:
        await _make(repo, 3)
        assert await repo.list_sessions(limit=10, offset=100) == []


class TestIndex:
    @pytest.mark.asyncio
    async def test_listing_needs_no_temp_sort(self, db: Any) -> None:
        """The paging query must be served by an index end to end.

        With only `(started_at DESC)` indexed, adding the `id` tiebreak made
        SQLite build a temp B-tree on every page — correctness bought at the
        cost of a sort per request.
        """
        rows = await db.fetch_all(
            "EXPLAIN QUERY PLAN SELECT * FROM sessions "
            "ORDER BY started_at DESC, id DESC LIMIT 50 OFFSET 100"
        )
        plan = " ".join(str(row[-1]) for row in rows)
        assert "idx_sessions_started_id" in plan
        assert "TEMP B-TREE" not in plan.upper()

    @pytest.mark.asyncio
    async def test_the_superseded_index_is_removed(self, db: Any) -> None:
        """`CREATE INDEX IF NOT EXISTS` cannot redefine an index that is already
        there, so the replacement carries a new name and drops the old one —
        otherwise existing databases keep the worse plan for ever."""
        names = {
            str(row[0])
            for row in await db.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'sessions'"
            )
        }
        assert "idx_sessions_started_id" in names
        assert "idx_sessions_started" not in names


class TestCount:
    @pytest.mark.asyncio
    async def test_counts_what_the_filters_match(self, repo: Repository) -> None:
        """The count is what the client pages against.

        Taken over a different set than the page, "Load more" either stops early
        or never stops. It used to ignore every filter.
        """
        await _make(repo, 5, tags=["standup"])
        await _make(repo, 0)
        for index in range(5, 12):
            await repo.create_session(
                SessionRecord(
                    id=f"other_{index}",
                    started_at=1_600_000_000_000 - index,
                    state=SessionState.ENDED,
                    title="Other",
                )
            )

        assert await repo.count_sessions() == 12
        assert await repo.count_sessions(tag="standup") == 5
        assert await repo.count_sessions(query="Other") == 7
        assert await repo.count_sessions(tag="nonexistent") == 0

    @pytest.mark.asyncio
    async def test_count_agrees_with_the_listing(self, repo: Repository) -> None:
        await _make(repo, 8, tags=["daily"])
        listed = await repo.list_sessions(limit=200, tag="daily")
        assert len(listed) == await repo.count_sessions(tag="daily")


class TestFacets:
    @pytest.mark.asyncio
    async def test_covers_sessions_beyond_the_first_page(self, repo: Repository) -> None:
        """A tag used only on an old session must still be offered as a filter,
        or the filter cannot reach the thing it exists to find."""
        await _make(repo, 60, tags=["common"], source_languages=["en"])
        await repo.create_session(
            SessionRecord(
                id="sess_ancient",
                started_at=1_500_000_000_000,  # oldest, so it sorts last
                state=SessionState.ENDED,
                title="Old one",
                tags=["rare"],
                source_languages=["sr"],
                target_language="ru",
            )
        )
        facets = await repo.session_facets()
        assert "rare" in facets["tags"]
        assert "sr" in facets["languages"]
        assert "ru" in facets["languages"]  # target languages count too

    @pytest.mark.asyncio
    async def test_empty_database_has_empty_facets(self, repo: Repository) -> None:
        assert await repo.session_facets() == {"languages": [], "tags": []}


class TestEndpoint:
    def test_reports_the_page_and_whether_more_remain(self, client: Any) -> None:
        for index in range(7):
            client.post("/api/sessions", json={"title": f"S{index}"})
        for session in client.get("/api/sessions?limit=100").json()["sessions"]:
            client.post(f"/api/sessions/{session['id']}/stop")

        first = client.get("/api/sessions?limit=3&offset=0").json()
        assert len(first["sessions"]) == 3
        assert first["total"] == 7
        assert first["has_more"] is True

        last = client.get("/api/sessions?limit=3&offset=6").json()
        assert len(last["sessions"]) == 1
        assert last["has_more"] is False

    def test_an_exactly_full_last_page_is_not_reported_as_more(self, client: Any) -> None:
        """`has_more` exists precisely so the client does not have to infer the
        end from a short page — which is wrong when the last page is full."""
        for index in range(4):
            client.post("/api/sessions", json={"title": f"S{index}"})
        page = client.get("/api/sessions?limit=2&offset=2").json()
        assert len(page["sessions"]) == 2
        assert page["has_more"] is False

    def test_total_follows_the_filter(self, client: Any) -> None:
        client.post("/api/sessions", json={"title": "Tagged", "tags": ["standup"]})
        client.post("/api/sessions", json={"title": "Untagged"})
        assert client.get("/api/sessions").json()["total"] == 2
        assert client.get("/api/sessions?tag=standup").json()["total"] == 1

    def test_facets_are_returned_for_the_filter_controls(self, client: Any) -> None:
        client.post("/api/sessions", json={"title": "One", "tags": ["weekly"]})
        facets = client.get("/api/sessions?limit=1").json()["facets"]
        assert "weekly" in facets["tags"]
        assert "en" in facets["languages"]
