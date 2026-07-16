from __future__ import annotations

from collections.abc import Mapping


def _serialize_phase_request(
    row: Mapping[str, object], vote_summary: dict[str, object]
) -> dict[str, object]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "from_phase_id": row["from_phase_id"],
        "target_phase_id": row["target_phase_id"],
        "change_kind": row["change_kind"],
        "close_outcome": row["close_outcome"],
        "conversion_target_mode": row["conversion_target_mode"],
        "conversion_target_subtype": row["conversion_target_subtype"],
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
        "project_id": row["project_id"],
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
        "project_id": row["project_id"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "vote_summary": vote_summary,
    }
