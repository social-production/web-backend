from __future__ import annotations

from collections.abc import Mapping


def _serialize_phase_request(
    row: Mapping[str, object], vote_summary: dict[str, object]
) -> dict[str, object]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "from_phase_id": row["from_phase_id"],
        "target_phase_id": row["target_phase_id"],
        "change_kind": row["change_kind"],
        "reason": row["reason"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def _serialize_update_request(
    row: Mapping[str, object], vote_summary: dict[str, object]
) -> dict[str, object]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "body": row["body"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }


def _serialize_edit_request(
    row: Mapping[str, object], vote_summary: dict[str, object]
) -> dict[str, object]:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }
