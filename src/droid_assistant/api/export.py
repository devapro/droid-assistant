"""Session export: Markdown, JSON, SRT, VTT, plain text (FR-EXP-1 … FR-EXP-3).

JSON is the fidelity format — it round-trips (FR-EXP-2), so it carries the
fields the others drop: word timings, confidences, embeddings excluded by size,
edit provenance, and every artifact version.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain import Speaker, Utterance
from ..store.repository import SessionRecord

SCHEMA_VERSION = 1


def _clock(ms: int, *, sep: str = ",") -> str:
    ms = max(0, ms)
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def to_markdown(
    session: SessionRecord,
    utterances: list[Utterance],
    speakers: dict[str, Speaker],
    artifacts: list[dict[str, Any]],
) -> str:
    lines = [f"# {session.title or 'Untitled session'}", ""]
    meta = [
        f"- **Recorded:** {_iso(session.started_at)}",
        f"- **Duration:** {session.duration_ms // 60000} min {session.duration_ms // 1000 % 60} s",
        f"- **Mode:** {session.mode}",
        f"- **Languages:** {', '.join(session.source_languages) or 'auto'}"
        f" → {session.target_language}",
        f"- **Speakers:** {len(speakers) or 'unattributed'}",
    ]
    if session.tags:
        meta.append(f"- **Tags:** {', '.join('#' + t for t in session.tags)}")
    if session.participants:
        meta.append(f"- **Participants:** {', '.join(session.participants)}")
    if session.cloud_used:
        meta.append(
            f"- **Cloud services used:** {', '.join(session.providers_used) or 'yes'}"
            + (f" (${session.cost_usd:.4f})" if session.cost_usd else "")
        )
    lines += [*meta, ""]

    current = [a for a in artifacts if a.get("current")]
    for artifact in current:
        lines += [f"## {artifact['kind'].replace('_', ' ').title()}", "", artifact["content"], ""]

    lines += ["## Transcript", ""]
    last_speaker: str | None = None
    for utt in utterances:
        speaker = speakers.get(utt.speaker_id or "")
        name = speaker.name if speaker else "Unknown"
        if name != last_speaker:
            lines.append(f"**{name}** · {_clock(utt.start_ms, sep='.')}")
            last_speaker = name
        marker = " ✎" if utt.edited_at else ""
        flag = " ⚑" if utt.marked else ""
        lines.append(f"{utt.text}{marker}{flag}")
        if utt.translation:
            lines.append(f"> {utt.translation}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_json(
    session: SessionRecord,
    utterances: list[Utterance],
    speakers: dict[str, Speaker],
    artifacts: list[dict[str, Any]],
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "session": {
            **session.to_json(),
            "started_at_iso": _iso(session.started_at),
            "ended_at_iso": _iso(session.ended_at),
        },
        "speakers": [
            {
                "id": s.id,
                "label": s.label,
                "index": s.index,
                "display_name": s.display_name,
            }
            for s in speakers.values()
        ],
        "utterances": [
            {
                "id": u.id,
                "seq": u.seq,
                "start_ms": u.start_ms,
                "end_ms": u.end_ms,
                "speaker_id": u.speaker_id,
                "language": u.language,
                "text": u.text,
                "text_original": u.text_original,
                "translation": u.translation,
                "translation_state": u.translation_state,
                "confidence": u.confidence,
                "marked": u.marked,
                "edited_at": u.edited_at,
                "words": [w.to_json() for w in u.words],
                "timings": u.timings,
            }
            for u in utterances
        ],
        "artifacts": [{k: v for k, v in a.items() if k != "session_id"} for a in artifacts],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def to_srt(
    utterances: list[Utterance], speakers: dict[str, Speaker], *, translated: bool = False
) -> str:
    blocks: list[str] = []
    index = 0
    for utt in utterances:
        text = (utt.translation if translated else utt.text) or utt.text
        if not text.strip():
            continue
        index += 1
        speaker = speakers.get(utt.speaker_id or "")
        prefix = f"{speaker.name}: " if speaker else ""
        blocks.append(
            f"{index}\n{_clock(utt.start_ms)} --> {_clock(max(utt.end_ms, utt.start_ms + 500))}\n"
            f"{prefix}{text.strip()}\n"
        )
    return "\n".join(blocks)


def to_vtt(
    utterances: list[Utterance], speakers: dict[str, Speaker], *, translated: bool = False
) -> str:
    lines = ["WEBVTT", ""]
    for utt in utterances:
        text = (utt.translation if translated else utt.text) or utt.text
        if not text.strip():
            continue
        speaker = speakers.get(utt.speaker_id or "")
        prefix = f"<v {speaker.name}>" if speaker else ""
        lines.append(
            f"{_clock(utt.start_ms, sep='.')} --> "
            f"{_clock(max(utt.end_ms, utt.start_ms + 500), sep='.')}"
        )
        lines.append(f"{prefix}{text.strip()}")
        lines.append("")
    return "\n".join(lines)


def to_text(utterances: list[Utterance], speakers: dict[str, Speaker]) -> str:
    out: list[str] = []
    for utt in utterances:
        speaker = speakers.get(utt.speaker_id or "")
        out.append(f"{speaker.name if speaker else 'Unknown'}: {utt.text}")
    return "\n".join(out) + "\n"


MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "srt": "application/x-subrip; charset=utf-8",
    "vtt": "text/vtt; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


def filename(session: SessionRecord, fmt: str) -> str:
    import re

    stem = re.sub(r"[^\w\- ]+", "", session.title or session.id).strip().replace(" ", "-")
    return f"{stem or session.id}.{fmt}"
