"""Full-text search over transcripts *and* artifacts (FR-SES-10).

Both indexes are queried, results are interleaved by rank, and each hit carries
enough context for the History screen to render a match row (SRS §5.1) — the
line, its speaker, its timestamp, and the session it belongs to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from .db import Database

# FTS5 treats these as syntax. A user typing `budget: Q3` means the words, not a
# column filter, so every term is quoted and the operators never reach the parser.
_TERM = re.compile(r'[^\s"()*:^-]+')

HIGHLIGHT_OPEN = "⟦"  # ⟦ — matches the SRS §5.1 wireframe
HIGHLIGHT_CLOSE = "⟧"


def to_match_query(raw: str, *, prefix: bool = True) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    A quoted phrase in the input stays a phrase; everything else becomes an
    implicit AND of quoted terms, with the final term made a prefix so that
    search-as-you-type finds `migrat` before the user finishes `migration`.
    """
    phrases = re.findall(r'"([^"]+)"', raw)
    rest = re.sub(r'"[^"]*"', " ", raw)
    terms = _TERM.findall(rest)
    parts = [f'"{p}"' for p in phrases if p.strip()]
    for i, term in enumerate(terms):
        last = i == len(terms) - 1
        parts.append(f'"{term}"*' if prefix and last and not phrases else f'"{term}"')
    return " AND ".join(parts)


@dataclass(slots=True)
class SearchHit:
    kind: Literal["utterance", "artifact"]
    session_id: str
    session_title: str | None
    session_started_at: int
    snippet: str
    rank: float
    utterance_id: str | None = None
    start_ms: int | None = None
    speaker: str | None = None
    artifact_id: str | None = None
    artifact_kind: str | None = None
    plugin_name: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "session_title": self.session_title,
            "session_started_at": self.session_started_at,
            "snippet": self.snippet,
            "rank": self.rank,
            "utterance_id": self.utterance_id,
            "start_ms": self.start_ms,
            "speaker": self.speaker,
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "plugin_name": self.plugin_name,
        }


class SearchIndex:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def search(
        self,
        query: str,
        *,
        limit: int = 50,
        session_id: str | None = None,
        include_artifacts: bool = True,
    ) -> list[SearchHit]:
        match = to_match_query(query)
        if not match:
            return []

        hits: list[SearchHit] = []
        session_filter = " AND u.session_id = ?" if session_id else ""
        params: list[Any] = [
            HIGHLIGHT_OPEN,
            HIGHLIGHT_CLOSE,
            HIGHLIGHT_OPEN,
            HIGHLIGHT_CLOSE,
            match,
        ]
        if session_id:
            params.append(session_id)
        params.append(limit)

        rows = await self.db.fetch_all(
            f"""
            SELECT u.id            AS utterance_id,
                   u.session_id    AS session_id,
                   u.start_ms      AS start_ms,
                   coalesce(sp.display_name, sp.label) AS speaker,
                   s.title         AS session_title,
                   s.started_at    AS session_started_at,
                   snippet(utterances_fts, 0, ?, ?, '…', 12) AS snip_text,
                   snippet(utterances_fts, 1, ?, ?, '…', 12) AS snip_translation,
                   bm25(utterances_fts) AS rank
              FROM utterances_fts
              JOIN utterances u ON u.rowid = utterances_fts.rowid
              JOIN sessions   s ON s.id = u.session_id
              LEFT JOIN speakers sp ON sp.id = u.speaker_id
             WHERE utterances_fts MATCH ?{session_filter}
             ORDER BY rank
             LIMIT ?
            """,
            params,
        )
        for r in rows:
            snippet = r["snip_text"] or ""
            if HIGHLIGHT_OPEN not in snippet and r["snip_translation"]:
                snippet = r["snip_translation"]
            hits.append(
                SearchHit(
                    kind="utterance",
                    session_id=r["session_id"],
                    session_title=r["session_title"],
                    session_started_at=r["session_started_at"],
                    snippet=snippet,
                    rank=float(r["rank"]),
                    utterance_id=r["utterance_id"],
                    start_ms=r["start_ms"],
                    speaker=r["speaker"],
                )
            )

        if include_artifacts:
            a_filter = " AND a.session_id = ?" if session_id else ""
            a_params: list[Any] = [HIGHLIGHT_OPEN, HIGHLIGHT_CLOSE, match]
            if session_id:
                a_params.append(session_id)
            a_params.append(limit)
            rows = await self.db.fetch_all(
                f"""
                SELECT a.id         AS artifact_id,
                       a.session_id AS session_id,
                       a.kind       AS artifact_kind,
                       a.plugin_name AS plugin_name,
                       s.title      AS session_title,
                       s.started_at AS session_started_at,
                       snippet(artifacts_fts, 0, ?, ?, '…', 16) AS snip,
                       bm25(artifacts_fts) AS rank
                  FROM artifacts_fts
                  JOIN artifacts a ON a.rowid = artifacts_fts.rowid
                  JOIN sessions  s ON s.id = a.session_id
                 WHERE artifacts_fts MATCH ? AND a.superseded_by IS NULL{a_filter}
                 ORDER BY rank
                 LIMIT ?
                """,
                a_params,
            )
            hits.extend(
                SearchHit(
                    kind="artifact",
                    session_id=r["session_id"],
                    session_title=r["session_title"],
                    session_started_at=r["session_started_at"],
                    snippet=r["snip"] or "",
                    rank=float(r["rank"]),
                    artifact_id=r["artifact_id"],
                    artifact_kind=r["artifact_kind"],
                    plugin_name=r["plugin_name"],
                )
                for r in rows
            )

        # bm25 returns a negative score, most relevant first.
        hits.sort(key=lambda h: h.rank)
        return hits[:limit]

    async def rebuild(self) -> None:
        """Rebuild both indexes from their content tables. Used by `doctor --fix`
        and after a bulk import, where the triggers were bypassed."""
        for table in ("utterances_fts", "artifacts_fts"):
            await self.db.execute(f"INSERT INTO {table}({table}) VALUES ('rebuild')")
