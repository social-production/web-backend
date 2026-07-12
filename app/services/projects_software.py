from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError
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
    projects,
    users,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.utils.votes import required_votes, resolve_project_vote_population

APPROVAL_THRESHOLD_PERCENT = 66.0
APPROVAL_THRESHOLD_RATIO = 0.66
VALID_VOTES = frozenset({"yes", "no"})
VALID_ACTIONS = frozenset({"grant", "revoke"})

PR_STAGE_LABELS: dict[str, str] = {
    "approval": "Approval",
    "awaiting-merge": "Awaiting merge",
    "confirmation": "Awaiting confirmation",
    "confirmed": "Merged",
    "rejected": "Rejected",
    "replaced": "Replaced",
}

_TABLES_READY = False


def _ensure_software_tables(db: Session) -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return

    ddl = [
        """
        CREATE TABLE IF NOT EXISTS project_pull_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            decision_id UUID NULL,
            title VARCHAR(200) NOT NULL,
            summary TEXT NOT NULL,
            pull_request_id VARCHAR(120) NOT NULL,
            pull_request_url TEXT NOT NULL,
            author_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            stage VARCHAR(24) NOT NULL DEFAULT 'approval',
            merge_id VARCHAR(120) NULL,
            merged_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            approval_threshold_percent NUMERIC(5, 2) NOT NULL DEFAULT 66.00,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_pull_request_votes (
            request_id UUID NOT NULL REFERENCES project_pull_requests(id) ON DELETE CASCADE,
            voter_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vote VARCHAR(8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (request_id, voter_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_merge_capability_members (
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_label VARCHAR(120) NOT NULL DEFAULT 'approved-request',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_merge_capability_change_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            decision_id UUID NOT NULL,
            action VARCHAR(8) NOT NULL,
            target_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            author_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'open',
            approval_threshold_percent NUMERIC(5, 2) NOT NULL DEFAULT 66.00,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_merge_capability_change_votes (
            request_id UUID NOT NULL REFERENCES project_merge_capability_change_requests(id) ON DELETE CASCADE,
            voter_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vote VARCHAR(8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (request_id, voter_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_repository_replacement_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            decision_id UUID NOT NULL,
            repository_url TEXT NOT NULL,
            previous_repository_url TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            related_pull_request_id UUID NOT NULL REFERENCES project_pull_requests(id) ON DELETE CASCADE,
            author_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'open',
            approval_threshold_percent NUMERIC(5, 2) NOT NULL DEFAULT 66.00,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_repository_replacement_votes (
            request_id UUID NOT NULL REFERENCES project_repository_replacement_requests(id) ON DELETE CASCADE,
            voter_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vote VARCHAR(8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (request_id, voter_id)
        )
        """,
    ]

    for statement in ddl:
        db.execute(text(statement))
    db.commit()
    _TABLES_READY = True


def _get_project_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    from app.services.projects.helpers import _resolve_effective_project_subtype

    effective_subtype = _resolve_effective_project_subtype(db, row["id"], row["project_subtype"])
    if effective_subtype != "software":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Software governance requires a software project",
        )
    return row


def _get_membership(db: Session, project_id: UUID, user_id: UUID) -> Mapping[str, object] | None:
    return db.execute(
        select(project_memberships).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).mappings().first()


def _ensure_member(db: Session, project_id: UUID, user_id: UUID) -> None:
    if _get_membership(db, project_id, user_id) is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can perform this action")


def _is_merge_capable(db: Session, project_id: UUID, user_id: UUID) -> bool:
    row = db.execute(
        select(project_merge_capability_members.c.user_id).where(
            project_merge_capability_members.c.project_id == project_id,
            project_merge_capability_members.c.user_id == user_id,
        )
    ).first()
    return row is not None


def _username_map(db: Session, user_ids: set[UUID]) -> dict[UUID, str]:
    if not user_ids:
        return {}
    rows = db.execute(select(users.c.id, users.c.username).where(users.c.id.in_(list(user_ids)))).all()
    return {row[0]: row[1] for row in rows}


def _detail_member_map(db: Session, user_ids: set[UUID]) -> dict[UUID, dict[str, object]]:
    if not user_ids:
        return {}
    rows = db.execute(select(users.c.id, users.c.username, users.c.bio).where(users.c.id.in_(list(user_ids)))).all()
    return {
        row[0]: {
            "id": str(row[0]),
            "username": row[1],
            "bio": row[2] or "",
        }
        for row in rows
    }


def _vote_rows(db: Session, table_obj, request_id: UUID) -> list[Mapping[str, object]]:
    return db.execute(select(table_obj).where(table_obj.c.request_id == request_id)).mappings().all()


def _compute_vote_summary(
    vote_rows: list[Mapping[str, object]],
    member_count: int,
    current_user_id: UUID | None,
) -> tuple[dict[str, object], bool, bool]:
    yes_count = 0
    no_count = 0
    active_vote: str | None = None

    for row in vote_rows:
        vote_value = str(row["vote"]).lower()
        if vote_value == "yes":
            yes_count += 1
        elif vote_value == "no":
            no_count += 1

        if current_user_id is not None and row["voter_id"] == current_user_id:
            active_vote = vote_value

    total_votes = yes_count + no_count
    eligible_voter_count = max(member_count, 0)
    votes_required = required_votes(eligible_voter_count)
    approval_ratio = (yes_count / total_votes) if total_votes > 0 else 0.0
    approval_percent = approval_ratio * 100.0
    meets_quorum = total_votes >= votes_required
    passes = meets_quorum and approval_ratio >= APPROVAL_THRESHOLD_RATIO

    remaining_eligible_votes = max(0, eligible_voter_count - total_votes)
    max_yes = yes_count + remaining_eligible_votes
    max_total = total_votes + remaining_eligible_votes
    can_meet_approval = (max_yes / max_total) >= APPROVAL_THRESHOLD_RATIO if max_total > 0 else False
    can_meet_quorum = max_total >= votes_required
    can_still_pass = (not passes) and can_meet_approval and can_meet_quorum

    quorum_threshold_percent = (votes_required / eligible_voter_count * 100.0) if eligible_voter_count > 0 else 0.0

    summary = {
        "yesCount": yes_count,
        "noCount": no_count,
        "totalVotes": total_votes,
        "approvalPercent": approval_percent,
        "activeVote": active_vote,
        "meetsQuorum": meets_quorum,
        "eligibleVoterCount": eligible_voter_count,
        "quorumThresholdPercent": quorum_threshold_percent,
        "votesRequired": votes_required,
        "votesRemaining": max(0, votes_required - total_votes),
        "remainingEligibleVotes": remaining_eligible_votes,
    }
    return summary, passes, can_still_pass


def _stage_label(stage: str) -> str:
    return PR_STAGE_LABELS.get(stage, stage.replace("-", " ").title())


def _governance_payload(db: Session, project_row: Mapping[str, object], current_user_id: UUID | None) -> dict[str, object]:
    project_id = project_row["id"]
    vote_context_population = resolve_project_vote_population(
        db,
        project_id,
        bool(project_row.get("is_platform_tagged")),
    )

    viewer_is_member = current_user_id is not None and _get_membership(db, project_id, current_user_id) is not None
    viewer_can_request_merge = viewer_is_member and current_user_id is not None and _is_merge_capable(db, project_id, current_user_id)

    leading_plan = db.execute(
        select(project_plans.c.repository_url, project_plans.c.plan_payload)
        .where(project_plans.c.project_id == project_id, project_plans.c.is_leading.is_(True))
        .limit(1)
    ).mappings().first()

    repository_url = str(leading_plan["repository_url"] or "") if leading_plan else ""
    plan_payload = dict(leading_plan["plan_payload"] or {}) if leading_plan else {}
    license_label = str(plan_payload.get("licenseLabel") or "Unspecified")

    merge_member_rows = db.execute(
        select(project_merge_capability_members).where(project_merge_capability_members.c.project_id == project_id)
    ).mappings().all()
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

    pr_rows = db.execute(
        select(project_pull_requests)
        .where(project_pull_requests.c.project_id == project_id)
        .order_by(project_pull_requests.c.created_at.desc())
    ).mappings().all()

    pull_requests: list[dict[str, object]] = []
    replaceable_pull_requests: list[dict[str, object]] = []
    for row in pr_rows:
        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_pull_request_votes, row["id"]),
            vote_context_population,
            current_user_id if viewer_is_member else None,
        )
        author_name = username_map.get(row["author_id"], "unknown")
        merged_by_name = username_map.get(row["merged_by_user_id"]) if row["merged_by_user_id"] is not None else None

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
                "viewerCanRecordMerge": viewer_can_request_merge and row["stage"] == "awaiting-merge",
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

    merge_request_rows = db.execute(
        select(project_merge_capability_change_requests)
        .where(project_merge_capability_change_requests.c.project_id == project_id)
        .order_by(project_merge_capability_change_requests.c.created_at.desc())
    ).mappings().all()

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
                "actionLabel": "Grant merge capability" if row["action"] == "grant" else "Revoke merge capability",
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

    repo_request_rows = db.execute(
        select(project_repository_replacement_requests)
        .where(project_repository_replacement_requests.c.project_id == project_id)
        .order_by(project_repository_replacement_requests.c.created_at.desc())
    ).mappings().all()

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
        "viewerCanRequestMergeCapabilityChanges": viewer_is_member and not bool(project_row.get("is_platform_tagged")),
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


def submit_pull_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    title: str,
    summary: str,
    pull_request_id: str,
    pull_request_url: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    try:
        db.execute(
            insert(project_pull_requests).values(
                id=uuid4(),
                project_id=project_row["id"],
                decision_id=uuid4(),
                title=title.strip(),
                summary=summary.strip(),
                pull_request_id=pull_request_id.strip(),
                pull_request_url=pull_request_url.strip(),
                author_id=current_user_id,
                stage="approval",
                merge_id=None,
                merged_by_user_id=None,
                approval_threshold_percent=Decimal("66.00"),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not submit pull request") from exc

    return _governance_payload(db, project_row, current_user_id)


def vote_pull_request(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="vote must be one of: ['no', 'yes']")

    request_row = db.execute(
        select(project_pull_requests).where(
            project_pull_requests.c.id == request_id,
            project_pull_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pull request not found")
    if request_row["stage"] not in {"approval", "confirmation"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pull request is not open for voting")

    existing = db.execute(
        select(project_pull_request_votes).where(
            project_pull_request_votes.c.request_id == request_id,
            project_pull_request_votes.c.voter_id == current_user_id,
        )
    ).mappings().first()

    try:
        if existing is None:
            db.execute(
                insert(project_pull_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_pull_request_votes)
                .where(
                    project_pull_request_votes.c.request_id == request_id,
                    project_pull_request_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_pull_request_votes, request_id),
            resolve_project_vote_population(db, project_row["id"], bool(project_row.get("is_platform_tagged"))),
            current_user_id,
        )

        next_stage = request_row["stage"]
        previous_stage = request_row["stage"]
        if request_row["stage"] == "approval":
            if passes:
                next_stage = "awaiting-merge"
            elif not can_still_pass:
                next_stage = "rejected"
        elif request_row["stage"] == "confirmation":
            if passes:
                next_stage = "confirmed"
            elif not can_still_pass:
                next_stage = "awaiting-merge"

        if next_stage != request_row["stage"]:
            db.execute(
                update(project_pull_requests)
                .where(project_pull_requests.c.id == request_id)
                .values(stage=next_stage)
            )
            if previous_stage == "confirmation" and next_stage == "awaiting-merge":
                db.execute(
                    update(project_pull_requests)
                    .where(project_pull_requests.c.id == request_id)
                    .values(merge_id=None, merged_by_user_id=None)
                )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "pull-request", "target_id": str(request_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record vote") from exc

    if previous_stage == "approval" and next_stage == "awaiting-merge" and request_row["author_id"] is not None:
        create_notification(
            db=db,
            recipient_id=request_row["author_id"],
            actor_id=current_user_id,
            kind="pr-approved",
            surface="project",
            subject_type="pull-request",
            subject_id=request_id,
            target_id=project_row["id"],
            title="Pull request approved",
            body="Voting passed and your pull request is approved for merge.",
            href=f"/projects/{project_row['slug']}/software",
        )

    return _governance_payload(db, project_row, current_user_id)


def record_pull_request_merge(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    merge_id: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    if not _is_merge_capable(db, project_row["id"], current_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only members with merge capability can record merges",
        )

    request_row = db.execute(
        select(project_pull_requests).where(
            project_pull_requests.c.id == request_id,
            project_pull_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pull request not found")
    if request_row["stage"] != "awaiting-merge":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Pull request must be approved before merge recording")

    try:
        db.execute(
            update(project_pull_requests)
            .where(project_pull_requests.c.id == request_id)
            .values(stage="confirmation", merge_id=merge_id.strip(), merged_by_user_id=current_user_id)
        )
        db.execute(
            delete(project_pull_request_votes).where(project_pull_request_votes.c.request_id == request_id)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record merge") from exc

    return _governance_payload(db, project_row, current_user_id)


def request_merge_capability_change(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    target_user_id: UUID,
    action: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)
    _ensure_member(db, project_row["id"], target_user_id)

    normalized_action = action.strip().lower()
    if normalized_action not in VALID_ACTIONS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="action must be one of: ['grant', 'revoke']")

    try:
        db.execute(
            insert(project_merge_capability_change_requests).values(
                id=uuid4(),
                project_id=project_row["id"],
                decision_id=uuid4(),
                action=normalized_action,
                target_user_id=target_user_id,
                author_id=current_user_id,
                status="open",
                approval_threshold_percent=Decimal("66.00"),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create merge capability request") from exc

    return _governance_payload(db, project_row, current_user_id)


def vote_merge_capability_change(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="vote must be one of: ['no', 'yes']")

    request_row = db.execute(
        select(project_merge_capability_change_requests).where(
            project_merge_capability_change_requests.c.id == request_id,
            project_merge_capability_change_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Merge capability request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Request is already closed")

    existing = db.execute(
        select(project_merge_capability_change_votes).where(
            project_merge_capability_change_votes.c.request_id == request_id,
            project_merge_capability_change_votes.c.voter_id == current_user_id,
        )
    ).mappings().first()

    try:
        if existing is None:
            db.execute(
                insert(project_merge_capability_change_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_merge_capability_change_votes)
                .where(
                    project_merge_capability_change_votes.c.request_id == request_id,
                    project_merge_capability_change_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_merge_capability_change_votes, request_id),
            resolve_project_vote_population(db, project_row["id"], bool(project_row.get("is_platform_tagged"))),
            current_user_id,
        )

        if passes:
            if request_row["action"] == "grant":
                db.execute(
                    pg_insert(project_merge_capability_members)
                    .values(
                        project_id=project_row["id"],
                        user_id=request_row["target_user_id"],
                        source_label="approved-request",
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
                )
            else:
                db.execute(
                    delete(project_merge_capability_members).where(
                        project_merge_capability_members.c.project_id == project_row["id"],
                        project_merge_capability_members.c.user_id == request_row["target_user_id"],
                    )
                )
            db.execute(
                update(project_merge_capability_change_requests)
                .where(project_merge_capability_change_requests.c.id == request_id)
                .values(status="approved")
            )
        elif not can_still_pass:
            db.execute(
                update(project_merge_capability_change_requests)
                .where(project_merge_capability_change_requests.c.id == request_id)
                .values(status="rejected")
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "merge-capability-request", "target_id": str(request_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record vote") from exc

    return _governance_payload(db, project_row, current_user_id)


def request_repository_replacement(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    repository_url: str,
    reason: str,
    related_pull_request_id: UUID,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    related = db.execute(
        select(project_pull_requests).where(
            project_pull_requests.c.id == related_pull_request_id,
            project_pull_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if related is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Related pull request not found")

    leading_plan = db.execute(
        select(project_plans.c.repository_url)
        .where(project_plans.c.project_id == project_row["id"], project_plans.c.is_leading.is_(True))
        .limit(1)
    ).mappings().first()
    previous_repository_url = str((leading_plan or {}).get("repository_url") or "")

    try:
        db.execute(
            insert(project_repository_replacement_requests).values(
                id=uuid4(),
                project_id=project_row["id"],
                decision_id=uuid4(),
                repository_url=repository_url.strip(),
                previous_repository_url=previous_repository_url,
                reason=reason.strip(),
                related_pull_request_id=related_pull_request_id,
                author_id=current_user_id,
                status="open",
                approval_threshold_percent=Decimal("66.00"),
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create repository replacement request") from exc

    return _governance_payload(db, project_row, current_user_id)


def vote_repository_replacement(
    db: Session,
    current_user_id: UUID,
    project_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    _ensure_software_tables(db)
    project_row = _get_project_by_slug(db, project_slug)
    _ensure_member(db, project_row["id"], current_user_id)

    normalized_vote = vote.strip().lower()
    if normalized_vote not in VALID_VOTES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="vote must be one of: ['no', 'yes']")

    request_row = db.execute(
        select(project_repository_replacement_requests).where(
            project_repository_replacement_requests.c.id == request_id,
            project_repository_replacement_requests.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository replacement request not found")
    if request_row["status"] != "open":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Request is already closed")

    existing = db.execute(
        select(project_repository_replacement_votes).where(
            project_repository_replacement_votes.c.request_id == request_id,
            project_repository_replacement_votes.c.voter_id == current_user_id,
        )
    ).mappings().first()

    try:
        if existing is None:
            db.execute(
                insert(project_repository_replacement_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                )
            )
        else:
            db.execute(
                update(project_repository_replacement_votes)
                .where(
                    project_repository_replacement_votes.c.request_id == request_id,
                    project_repository_replacement_votes.c.voter_id == current_user_id,
                )
                .values(vote=normalized_vote)
            )

        vote_summary, passes, can_still_pass = _compute_vote_summary(
            _vote_rows(db, project_repository_replacement_votes, request_id),
            resolve_project_vote_population(db, project_row["id"], bool(project_row.get("is_platform_tagged"))),
            current_user_id,
        )

        if passes:
            db.execute(
                update(project_repository_replacement_requests)
                .where(project_repository_replacement_requests.c.id == request_id)
                .values(status="approved")
            )
            db.execute(
                update(project_plans)
                .where(project_plans.c.project_id == project_row["id"], project_plans.c.is_leading.is_(True))
                .values(repository_url=request_row["repository_url"])
            )
            db.execute(
                update(project_pull_requests)
                .where(project_pull_requests.c.id == request_row["related_pull_request_id"])
                .values(stage="replaced")
            )
            if request_row["author_id"] is not None:
                db.execute(
                    delete(project_merge_capability_members).where(
                        project_merge_capability_members.c.project_id == project_row["id"],
                        project_merge_capability_members.c.source_label.in_(["plan-creator", "repo-replacement"]),
                    )
                )
                db.execute(
                    pg_insert(project_merge_capability_members)
                    .values(
                        project_id=project_row["id"],
                        user_id=request_row["author_id"],
                        source_label="repo-replacement",
                    )
                    .on_conflict_do_nothing(index_elements=["project_id", "user_id"])
                )
        elif not can_still_pass:
            db.execute(
                update(project_repository_replacement_requests)
                .where(project_repository_replacement_requests.c.id == request_id)
                .values(status="rejected")
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "repository-replacement-request", "target_id": str(request_id), "vote": normalized_vote},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not record vote") from exc

    return _governance_payload(db, project_row, current_user_id)


def _set_merge_capability_members(
    db: Session,
    project_id: UUID,
    user_ids: list[UUID],
    source_label: str,
) -> None:
    _ensure_software_tables(db)
    db.execute(
        delete(project_merge_capability_members).where(
            project_merge_capability_members.c.project_id == project_id,
            project_merge_capability_members.c.source_label == source_label,
        )
    )
    for user_id in user_ids:
        db.execute(
            pg_insert(project_merge_capability_members)
            .values(project_id=project_id, user_id=user_id, source_label=source_label)
            .on_conflict_do_update(
                index_elements=["project_id", "user_id"],
                set_={"source_label": source_label},
            )
        )


def sync_merge_capability_for_leading_plan(db: Session, project_id: UUID, plan_id: UUID) -> None:
    _ensure_software_tables(db)
    project_row = db.execute(select(projects).where(projects.c.id == project_id)).mappings().first()
    if project_row is None or project_row["project_subtype"] != "software":
        return

    plan_row = db.execute(
        select(project_plans.c.author_id).where(
            project_plans.c.id == plan_id,
            project_plans.c.project_id == project_id,
            project_plans.c.is_leading.is_(True),
        )
    ).mappings().first()
    if plan_row is None:
        return

    if bool(project_row.get("is_platform_tagged")):
        sync_platform_software_merge_capability(db, project_id=project_id)
        return

    if plan_row["author_id"] is not None:
        _set_merge_capability_members(db, project_id, [plan_row["author_id"]], "plan-creator")


def sync_platform_software_merge_capability(db: Session, project_id: UUID | None = None) -> None:
    from app.services.board import get_active_board_member_ids

    _ensure_software_tables(db)
    board_member_ids = get_active_board_member_ids(db)

    query = select(projects.c.id).where(
        projects.c.project_subtype == "software",
        projects.c.is_platform_tagged.is_(True),
    )
    if project_id is not None:
        query = query.where(projects.c.id == project_id)

    project_ids = [row[0] for row in db.execute(query).all()]
    for pid in project_ids:
        db.execute(
            delete(project_merge_capability_members).where(
                project_merge_capability_members.c.project_id == pid,
                project_merge_capability_members.c.source_label == "platform-board",
            )
        )
        if board_member_ids:
            _set_merge_capability_members(db, pid, board_member_ids, "platform-board")


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

    pr_rows = db.execute(
        select(project_pull_requests)
        .where(project_pull_requests.c.project_id == project_id)
        .order_by(project_pull_requests.c.created_at.desc())
    ).mappings().all()

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
        if row["stage"] in {"confirmed", "awaiting-merge"} and kind == "project-pull-request-approval":
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
                    "kindLabel": "Merge confirmation" if kind.endswith("confirmation") else "Pull request approval",
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

    merge_rows = db.execute(
        select(project_merge_capability_change_requests)
        .where(project_merge_capability_change_requests.c.project_id == project_id)
        .order_by(project_merge_capability_change_requests.c.created_at.desc())
    ).mappings().all()
    for row in merge_rows:
        vote_summary, passes, can_still = _compute_vote_summary(
            _vote_rows(db, project_merge_capability_change_votes, row["id"]),
            vote_context_population,
            current_user_id,
        )
        target = db.execute(select(users.c.username).where(users.c.id == row["target_user_id"])).first()
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

    repo_rows = db.execute(
        select(project_repository_replacement_requests)
        .where(project_repository_replacement_requests.c.project_id == project_id)
        .order_by(project_repository_replacement_requests.c.created_at.desc())
    ).mappings().all()
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
