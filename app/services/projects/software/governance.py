from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    project_memberships,
    project_merge_capability_change_requests,
    project_merge_capability_change_votes,
    project_merge_capability_members,
    project_plans,
    project_pull_request_votes,
    project_pull_requests,
    project_repository_replacement_requests,
    project_repository_replacement_votes,
)
from app.services.governance_votes import compute_software_vote_summary
from app.services.projects.software.constants import APPROVAL_THRESHOLD_RATIO
from app.services.projects.software.helpers import (
    _detail_member_map,
    _ensure_software_tables,
    _get_membership,
    _get_project_by_slug,
    _is_merge_capable,
    _stage_label,
    _vote_rows,
)
from app.utils.votes import resolve_project_vote_population


def _compute_vote_summary(
    vote_rows: list[Mapping[str, object]],
    member_count: int,
    current_user_id: UUID | None,
) -> tuple[dict[str, object], bool, bool]:
    return compute_software_vote_summary(
        vote_rows,
        member_count,
        current_user_id,
        approval_threshold_ratio=APPROVAL_THRESHOLD_RATIO,
    )


def _governance_payload(
    db: Session, project_row: Mapping[str, object], current_user_id: UUID | None
) -> dict[str, object]:
    project_id = project_row["id"]
    vote_context_population = resolve_project_vote_population(
        db,
        project_id,
        bool(project_row.get("is_platform_tagged")),
    )

    viewer_is_member = (
        current_user_id is not None and _get_membership(db, project_id, current_user_id) is not None
    )
    viewer_can_request_merge = (
        viewer_is_member
        and current_user_id is not None
        and _is_merge_capable(db, project_id, current_user_id)
    )

    leading_plan = (
        db.execute(
            select(project_plans.c.repository_url, project_plans.c.plan_payload)
            .where(project_plans.c.project_id == project_id, project_plans.c.is_leading.is_(True))
            .limit(1)
        )
        .mappings()
        .first()
    )

    repository_url = str(leading_plan["repository_url"] or "") if leading_plan else ""
    plan_payload = dict(leading_plan["plan_payload"] or {}) if leading_plan else {}
    license_label = str(plan_payload.get("licenseLabel") or "Unspecified")

    merge_member_rows = (
        db.execute(
            select(project_merge_capability_members).where(
                project_merge_capability_members.c.project_id == project_id
            )
        )
        .mappings()
        .all()
    )
    merge_member_ids = {row["user_id"] for row in merge_member_rows}

    membership_rows = db.execute(
        select(project_memberships.c.user_id).where(project_memberships.c.project_id == project_id)
    ).all()
    member_ids = {row[0] for row in membership_rows}

    user_map = _detail_member_map(db, member_ids | merge_member_ids)
    username_map = {uid: info["username"] for uid, info in user_map.items()}

    merge_capability_members = [
        {
            "id": str(row["user_id"]),
            "username": str(user_map.get(row["user_id"], {}).get("username") or "unknown"),
            "bio": str(user_map.get(row["user_id"], {}).get("bio") or ""),
            "sourceLabel": row["source_label"],
        }
        for row in merge_member_rows
    ]

    available_merge_candidates = [
        user_map[user_id]
        for user_id in member_ids
        if user_id in user_map and user_id not in merge_member_ids
    ]

    pr_rows = (
        db.execute(
            select(project_pull_requests)
            .where(project_pull_requests.c.project_id == project_id)
            .order_by(project_pull_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    pull_requests: list[dict[str, object]] = []
    replaceable_pull_requests: list[dict[str, object]] = []
    for row in pr_rows:
        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_pull_request_votes, row["id"]),
            vote_context_population,
            current_user_id if viewer_is_member else None,
        )
        author_name = username_map.get(row["author_id"], "unknown")
        merged_by_name = (
            username_map.get(row["merged_by_user_id"])
            if row["merged_by_user_id"] is not None
            else None
        )

        pull_requests.append(
            {
                "id": str(row["id"]),
                "decisionId": str(row["decision_id"]) if row["decision_id"] is not None else None,
                "title": row["title"],
                "summary": row["summary"],
                "pullRequestId": row["pull_request_id"],
                "pullRequestUrl": row["pull_request_url"],
                "authorUsername": author_name,
                "createdAt": row["created_at"].isoformat(),
                "stage": row["stage"],
                "stageLabel": _stage_label(row["stage"]),
                "mergeId": row["merge_id"],
                "mergedByUsername": merged_by_name,
                "approvalThresholdPercent": float(row["approval_threshold_percent"]),
                "voteSummary": vote_summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still_pass,
                "viewerCanRecordMerge": viewer_can_request_merge
                and row["stage"] == "awaiting-merge",
                "viewerCanVote": viewer_is_member and row["stage"] in {"approval", "confirmation"},
            }
        )

        if row["stage"] == "awaiting-merge":
            replaceable_pull_requests.append(
                {
                    "id": str(row["id"]),
                    "title": row["title"],
                    "pullRequestId": row["pull_request_id"],
                    "stage": row["stage"],
                    "stageLabel": _stage_label(row["stage"]),
                }
            )

    merge_request_rows = (
        db.execute(
            select(project_merge_capability_change_requests)
            .where(project_merge_capability_change_requests.c.project_id == project_id)
            .order_by(project_merge_capability_change_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    merge_change_requests: list[dict[str, object]] = []
    for row in merge_request_rows:
        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_merge_capability_change_votes, row["id"]),
            vote_context_population,
            current_user_id if viewer_is_member else None,
        )
        target_member = user_map.get(
            row["target_user_id"],
            {"id": str(row["target_user_id"]), "username": "unknown", "bio": ""},
        )
        author_name = username_map.get(row["author_id"], "unknown")

        merge_change_requests.append(
            {
                "id": str(row["id"]),
                "decisionId": str(row["decision_id"]),
                "action": row["action"],
                "actionLabel": "Grant merge capability"
                if row["action"] == "grant"
                else "Revoke merge capability",
                "targetMember": target_member,
                "authorUsername": author_name,
                "createdAt": row["created_at"].isoformat(),
                "approvalThresholdPercent": float(row["approval_threshold_percent"]),
                "voteSummary": vote_summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still_pass,
                "viewerCanVote": viewer_is_member and row["status"] == "open" and not passes,
            }
        )

    repo_request_rows = (
        db.execute(
            select(project_repository_replacement_requests)
            .where(project_repository_replacement_requests.c.project_id == project_id)
            .order_by(project_repository_replacement_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    repository_requests: list[dict[str, object]] = []
    repository_history: list[dict[str, object]] = []
    for row in repo_request_rows:
        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_repository_replacement_votes, row["id"]),
            vote_context_population,
            current_user_id if viewer_is_member else None,
        )
        author_name = username_map.get(row["author_id"], "unknown")

        repository_requests.append(
            {
                "id": str(row["id"]),
                "decisionId": str(row["decision_id"]),
                "repositoryUrl": row["repository_url"],
                "previousRepositoryUrl": row["previous_repository_url"],
                "reason": row["reason"],
                "relatedPullRequestId": str(row["related_pull_request_id"]),
                "authorUsername": author_name,
                "createdAt": row["created_at"].isoformat(),
                "approvalThresholdPercent": float(row["approval_threshold_percent"]),
                "voteSummary": vote_summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still_pass,
                "viewerCanVote": viewer_is_member and row["status"] == "open" and not passes,
            }
        )

        if row["status"] == "approved":
            repository_history.append(
                {
                    "id": str(row["id"]),
                    "repositoryUrl": row["repository_url"],
                    "previousRepositoryUrl": row["previous_repository_url"],
                    "reason": row["reason"],
                    "relatedPullRequestId": str(row["related_pull_request_id"]),
                    "replacedAt": row["updated_at"].isoformat(),
                    "replacedByUsername": author_name,
                }
            )

    return {
        "repositoryUrl": repository_url,
        "licenseLabel": license_label,
        "isPlatformTagged": bool(project_row.get("is_platform_tagged")),
        "mergeCapabilityManagedByPlatform": bool(project_row.get("is_platform_tagged")),
        "mergeCapabilityMembers": merge_capability_members,
        "availableMergeCapabilityCandidates": available_merge_candidates,
        "mergeCapabilityChangeRequests": merge_change_requests,
        "repositoryReplacementRequests": repository_requests,
        "replaceablePullRequests": replaceable_pull_requests,
        "repositoryHistory": repository_history,
        "pullRequests": pull_requests,
        "viewerCanCreatePullRequests": viewer_is_member,
        "viewerCanRequestMergeCapabilityChanges": viewer_is_member
        and not bool(project_row.get("is_platform_tagged")),
        "viewerCanRequestRepositoryReplacement": viewer_is_member,
    }


def get_project_software_governance(
    db: Session,
    project_slug: str,
    current_user_id: UUID | None,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    return _governance_payload(db, project_row, current_user_id)
