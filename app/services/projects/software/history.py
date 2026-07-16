from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    project_merge_capability_change_requests,
    project_merge_capability_change_votes,
    project_pull_request_votes,
    project_pull_requests,
    project_repository_replacement_requests,
    project_repository_replacement_votes,
    users,
)
from app.services.projects.software.governance import _compute_vote_summary
from app.services.projects.software.helpers import (
    _ensure_software_tables,
    _vote_rows,
)


def build_software_history_entries(
    db: Session,
    project_id: UUID,
    vote_context_population: int,
    current_user_id: UUID | None,
) -> list[tuple[object, dict[str, object]]]:
    _ensure_software_tables(db)
    entries: list[tuple[object, dict[str, object]]] = []

    def _author_username(author_id: UUID | None) -> str:
        if author_id is None:
            return "unknown"
        row = db.execute(select(users.c.username).where(users.c.id == author_id)).first()
        return row[0] if row else "unknown"

    pr_rows = (
        db.execute(
            select(project_pull_requests)
            .where(project_pull_requests.c.project_id == project_id)
            .order_by(project_pull_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    for row in pr_rows:
        vote_summary, passes, can_still = _compute_vote_summary(
            _vote_rows(db, project_pull_request_votes, row["id"]),
            vote_context_population,
            current_user_id,
        )
        kind = (
            "project-pull-request-confirmation"
            if row["stage"] in {"confirmation", "confirmed"}
            else "project-pull-request-approval"
        )
        status = "open"
        if (
            row["stage"] in {"confirmed", "awaiting-merge"}
            and kind == "project-pull-request-approval"
        ):
            status = "approved"
        elif row["stage"] == "confirmed":
            status = "approved"
        elif row["stage"] == "rejected":
            status = "rejected"
        elif row["stage"] == "confirmation":
            status = "open"

        entries.append(
            (
                row["created_at"],
                {
                    "id": str(row["id"]),
                    "entityKind": "project",
                    "kind": kind,
                    "kindLabel": "Merge confirmation"
                    if kind.endswith("confirmation")
                    else "Pull request approval",
                    "createdAt": row["created_at"].isoformat(),
                    "authorUsername": _author_username(row["author_id"]),
                    "status": status,
                    "approvalThresholdPercent": float(row["approval_threshold_percent"]),
                    "voteSummary": vote_summary,
                    "passesApprovalThreshold": passes,
                    "canStillPass": can_still,
                    "canVote": row["stage"] in {"approval", "confirmation"},
                    "payload": {
                        "type": "pull-request",
                        "title": row["title"],
                        "summary": row["summary"],
                        "pullRequestId": row["pull_request_id"],
                        "pullRequestUrl": row["pull_request_url"],
                        "stage": row["stage"],
                        "mergeId": row["merge_id"],
                    },
                },
            )
        )

    merge_rows = (
        db.execute(
            select(project_merge_capability_change_requests)
            .where(project_merge_capability_change_requests.c.project_id == project_id)
            .order_by(project_merge_capability_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    for row in merge_rows:
        vote_summary, passes, can_still = _compute_vote_summary(
            _vote_rows(db, project_merge_capability_change_votes, row["id"]),
            vote_context_population,
            current_user_id,
        )
        target = db.execute(
            select(users.c.username).where(users.c.id == row["target_user_id"])
        ).first()
        entries.append(
            (
                row["created_at"],
                {
                    "id": str(row["id"]),
                    "entityKind": "project",
                    "kind": "project-merge-capability-change",
                    "kindLabel": "Merge capability change",
                    "createdAt": row["created_at"].isoformat(),
                    "authorUsername": _author_username(row["author_id"]),
                    "status": row["status"],
                    "approvalThresholdPercent": float(row["approval_threshold_percent"]),
                    "voteSummary": vote_summary,
                    "passesApprovalThreshold": passes,
                    "canStillPass": can_still,
                    "canVote": row["status"] == "open",
                    "payload": {
                        "type": "merge-capability-change",
                        "action": row["action"],
                        "targetUsername": target[0] if target else "unknown",
                    },
                },
            )
        )

    repo_rows = (
        db.execute(
            select(project_repository_replacement_requests)
            .where(project_repository_replacement_requests.c.project_id == project_id)
            .order_by(project_repository_replacement_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    for row in repo_rows:
        vote_summary, passes, can_still = _compute_vote_summary(
            _vote_rows(db, project_repository_replacement_votes, row["id"]),
            vote_context_population,
            current_user_id,
        )
        entries.append(
            (
                row["created_at"],
                {
                    "id": str(row["id"]),
                    "entityKind": "project",
                    "kind": "project-repository-replacement",
                    "kindLabel": "Repository replacement",
                    "createdAt": row["created_at"].isoformat(),
                    "authorUsername": _author_username(row["author_id"]),
                    "status": row["status"],
                    "approvalThresholdPercent": float(row["approval_threshold_percent"]),
                    "voteSummary": vote_summary,
                    "passesApprovalThreshold": passes,
                    "canStillPass": can_still,
                    "canVote": row["status"] == "open",
                    "payload": {
                        "type": "repository-replacement",
                        "repositoryUrl": row["repository_url"],
                        "previousRepositoryUrl": row["previous_repository_url"],
                        "reason": row["reason"],
                        "relatedPullRequestId": str(row["related_pull_request_id"]),
                    },
                },
            )
        )

    return entries
