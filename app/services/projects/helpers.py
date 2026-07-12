from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    channels,
    comments,
    communities,
    content_votes,
    project_activities,
    project_activity_assignments,
    project_activity_roles,
    project_edit_request_votes,
    project_edit_requests,
    project_link_request_votes,
    project_link_requests,
    project_links,
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_plan_criterion_ratings,
    project_plan_value_votes,
    project_plan_votes,
    project_plans,
    project_revert_history,
    project_service_request_setting_change_votes,
    project_service_request_setting_changes,
    project_service_request_settings,
    project_service_requests,
    project_signals,
    project_tags,
    project_update_request_votes,
    project_update_requests,
    project_updates,
    project_value_importance_votes,
    project_values,
    projects,
    reports,
    scope_memberships,
    user_follows,
    users,
)
from app.services.activity_history import (
    build_event_history_items,
    build_project_history_items,
    ensure_activity_roles_unlocked,
    ensure_future_scheduled_start,
    is_activity_ended,
    load_event_ratings_by_activity,
    load_project_ratings_by_activity,
    utc_now,
)
from app.cache import cache_ttl_seconds
from app.services.content import activity_status_tone
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.search import index_document
from app.services.projects_software import get_project_software_governance
from app.services.projects_plans import _plan_subtype_from_payload, _subtype_label
from app.services.plan_criteria import assessment_criteria_for_plan, serialize_plan_criterion_assessments
from app.utils.votes import required_votes, resolve_project_vote_population

PROJECT_MODES = frozenset({"productive", "collective-service", "personal-service"})
PROJECT_SUBTYPES = frozenset({"standard", "software"})
PROJECT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
PROJECT_PHASES = (
    ("phase-1", 1, "P1", "Proposal", "Define values and demand."),
    ("phase-2", 2, "P2", "Production Plan", "Select production plan."),
    ("phase-3", 3, "P3", "Distribution Plan", "Select distribution plan."),
    ("phase-4", 4, "P4", "Acquisition", "Prepare acquisition and inventory."),
    ("phase-5", 5, "P5", "Activity", "Run project activities."),
    ("phase-6", 6, "P6", "Pending Execution", "Await execution confirmation."),
    ("phase-7", 7, "P7", "Closed", "Project has closed."),
)


def _serialize_project(row: Mapping[str, object], tags: list[dict[str, object]], signal_counts: dict[str, int]) -> dict[str, object]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "project_mode": row["project_mode"],
        "project_subtype": row["project_subtype"],
        "current_phase_id": row["current_phase_id"],
        "stage_label": row["stage_label"],
        "location_label": row["location_label"],
        "is_platform_tagged": row["is_platform_tagged"],
        "is_closed": row["is_closed"],
        "close_outcome": row["close_outcome"],
        "signal_count": row["signal_count"],
        "vote_count": row["vote_count"],
        "comment_count": row["comment_count"],
        "member_count": row["member_count"],
        "last_activity_at": row["last_activity_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tags": tags,
        "signals": signal_counts,
    }


def _resolve_effective_project_subtype(
    db: Session,
    project_id: UUID,
    project_subtype: str | None,
) -> str | None:
    if project_subtype:
        return str(project_subtype)

    leading = db.execute(
        select(project_plans.c.project_subtype, project_plans.c.plan_payload)
        .where(
            project_plans.c.project_id == project_id,
            project_plans.c.is_leading.is_(True),
            project_plans.c.phase_kind.in_(("production", "organisation")),
        )
        .limit(1)
    ).mappings().first()
    if leading is None:
        return None

    return leading["project_subtype"] or _plan_subtype_from_payload(dict(leading["plan_payload"] or {}))


def _get_project_by_slug_row(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return row


def _ensure_personal_service_author(project_row: Mapping[str, object], user_id: UUID) -> None:
    if project_row["project_mode"] != "personal-service" or project_row["author_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the service creator can perform this action",
        )


def _resolve_channel_ids(db: Session, channel_slugs: list[str]) -> list[UUID]:
    normalized = [value.strip().lower() for value in channel_slugs if value.strip()]
    if not normalized:
        return []

    rows = db.execute(
        select(channels.c.id, channels.c.slug).where(channels.c.slug.in_(normalized))
    ).mappings().all()
    found = {row["slug"] for row in rows}
    missing = sorted(set(normalized) - found)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown channel slugs: {missing}",
        )

    return [row["id"] for row in rows]


def _resolve_community_ids(db: Session, community_slugs: list[str], current_user_id: UUID) -> list[UUID]:
    normalized = [value.strip().lower() for value in community_slugs if value.strip()]
    if not normalized:
        return []

    rows = db.execute(
        select(communities.c.id, communities.c.slug, communities.c.join_policy).where(communities.c.slug.in_(normalized))
    ).mappings().all()
    found = {row["slug"] for row in rows}
    missing = sorted(set(normalized) - found)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown community slugs: {missing}",
        )

    closed_ids = [row["id"] for row in rows if row["join_policy"] == "closed"]
    if closed_ids:
        membership_rows = db.execute(
            select(scope_memberships.c.scope_id).where(
                scope_memberships.c.scope_kind == "community",
                scope_memberships.c.scope_id.in_(closed_ids),
                scope_memberships.c.user_id == current_user_id,
            )
        ).all()
        member_ids = {row[0] for row in membership_rows}
        forbidden = sorted(row["slug"] for row in rows if row["id"] in closed_ids and row["id"] not in member_ids)
        if forbidden:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You must be a member to tag private communities: {forbidden}",
            )

    return [row["id"] for row in rows]


def _get_project_tags(db: Session, project_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            project_tags.c.id,
            project_tags.c.tag_kind,
            project_tags.c.channel_id,
            project_tags.c.community_id,
        ).where(project_tags.c.project_id == project_id)
    ).mappings().all()
    return [dict(row) for row in rows]


def _get_signal_counts_db(db: Session, project_id: UUID) -> dict[str, int]:
    grouped_rows = db.execute(
        select(project_signals.c.signal_type, func.count().label("count"))
        .where(project_signals.c.project_id == project_id)
        .group_by(project_signals.c.signal_type)
    ).all()

    demand = 0
    opposition = 0
    for signal_type, count in grouped_rows:
        if signal_type == "demand":
            demand = int(count)
        elif signal_type == "opposition":
            opposition = int(count)

    return {
        "demand": demand,
        "opposition": opposition,
        "total": demand + opposition,
    }


async def _write_signal_counts_cache(cache: Redis, project_id: UUID, counts: dict[str, int]) -> None:
    key = f"project:{project_id}:signals"
    await cache.hset(
        key,
        mapping={
            "demand": str(counts["demand"]),
            "opposition": str(counts["opposition"]),
            "total": str(counts["total"]),
        },
    )
    await cache.expire(key, cache_ttl_seconds())


async def _get_signal_counts(db: Session, cache: Redis, project_id: UUID) -> dict[str, int]:
    key = f"project:{project_id}:signals"
    cached = await cache.hgetall(key)
    if cached:
        return {
            "demand": int(cached.get("demand", 0)),
            "opposition": int(cached.get("opposition", 0)),
            "total": int(cached.get("total", 0)),
        }

    counts = _get_signal_counts_db(db, project_id)
    await _write_signal_counts_cache(cache, project_id, counts)
    return counts


def _phase_for_mode(project_mode: str) -> tuple[str, str]:
    from app.services.projects_phases import display_stage_label

    if project_mode == "personal-service":
        return "phase-1", display_stage_label("personal-service", None, "phase-1")
    return "phase-1", display_stage_label(project_mode, None, "phase-1")


def create_project(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    description: str,
    project_mode: str,
    project_subtype: str | None,
    location_label: str,
    channel_slugs: list[str],
    community_slugs: list[str] | None = None,
    request_mode: str | None = None,
) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    normalized_mode = project_mode.strip().lower()
    normalized_subtype = project_subtype.strip().lower() if project_subtype else None

    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")
    if normalized_mode not in PROJECT_MODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"project_mode must be one of: {sorted(PROJECT_MODES)}",
        )

    if normalized_mode == "personal-service":
        if normalized_subtype is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="personal-service projects must not include project_subtype",
            )
    elif normalized_subtype is not None and normalized_subtype not in PROJECT_SUBTYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"project_subtype must be one of: {sorted(PROJECT_SUBTYPES)} for non personal-service modes",
        )

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    community_ids = _resolve_community_ids(db, community_slugs or [], current_user_id)
    is_platform_tagged = "platform" in [value.strip().lower() for value in channel_slugs if value.strip()]
    normalized_request_mode = (request_mode or "both").strip().lower()
    if normalized_request_mode not in {"calendar", "direct", "both"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="request_mode must be one of: ['both', 'calendar', 'direct']",
        )
    if not channel_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one channel tag is required",
        )

    phase_id, stage_label = _phase_for_mode(normalized_mode)
    now = datetime.now(timezone.utc)

    try:
        created = db.execute(
            insert(projects)
            .values(
                slug=normalized_slug,
                title=title.strip(),
                description=description.strip(),
                author_id=current_user_id,
                project_mode=normalized_mode,
                project_subtype=normalized_subtype,
                current_phase_id=phase_id,
                stage_label=stage_label,
                location_label=location_label.strip(),
                member_count=1,
                last_activity_at=now,
                is_platform_tagged=is_platform_tagged,
            )
            .returning(
                projects.c.id,
                projects.c.slug,
                projects.c.title,
                projects.c.description,
                projects.c.author_id,
                projects.c.project_mode,
                projects.c.project_subtype,
                projects.c.current_phase_id,
                projects.c.stage_label,
                projects.c.location_label,
                projects.c.is_platform_tagged,
                projects.c.is_closed,
                projects.c.close_outcome,
                projects.c.signal_count,
                projects.c.vote_count,
                projects.c.comment_count,
                projects.c.member_count,
                projects.c.last_activity_at,
                projects.c.created_at,
                projects.c.updated_at,
            )
        ).mappings().one()

        db.execute(
            insert(project_memberships).values(
                project_id=created["id"],
                user_id=current_user_id,
                is_manager=normalized_mode == "personal-service",
                is_manager_candidate=False,
                joined_at=now,
            )
        )

        if normalized_mode == "personal-service":
            db.execute(
                insert(project_service_request_settings).values(
                    project_id=created["id"],
                    enabled=True,
                    request_mode=normalized_request_mode,
                    allow_off_schedule_requests=normalized_request_mode == "both",
                )
            )

        for channel_id in channel_ids:
            db.execute(
                insert(project_tags).values(
                    project_id=created["id"],
                    tag_kind="channel",
                    channel_id=channel_id,
                    community_id=None,
                )
            )

        for community_id in community_ids:
            db.execute(
                insert(project_tags).values(
                    project_id=created["id"],
                    tag_kind="community",
                    channel_id=None,
                    community_id=community_id,
                )
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-project",
            metadata={"project_id": str(created["id"]), "slug": created["slug"]},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Project slug already exists") from exc

    tags = _get_project_tags(db, created["id"])
    index_document(
        db=db,
        entity_type="project",
        entity_id=created["id"],
        title=created["title"],
        summary=created["description"],
        meta=created["project_mode"],
        href=f"/projects/{created['slug']}",
    )
    return {"project": _serialize_project(created, tags, {"demand": 0, "opposition": 0, "total": 0})}


async def get_project_by_slug(db: Session, cache: Redis, slug: str) -> dict[str, object]:
    row = _get_project_by_slug_row(db, slug)
    tags = _get_project_tags(db, row["id"])
    signal_counts = await _get_signal_counts(db, cache, row["id"])
    return {"project": _serialize_project(row, tags, signal_counts)}


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return datetime.now(timezone.utc).isoformat()


def _username_lookup(db: Session, user_ids: set[UUID]) -> dict[UUID, dict[str, str]]:
    if not user_ids:
        return {}
    rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url).where(
            users.c.id.in_(list(user_ids))
        )
    ).all()
    return {
        row[0]: {
            "username": row[1],
            "bio": row[2] or "",
            "profileImageUrl": row[3],
        }
        for row in rows
    }


def _vote_summary(
    vote_rows: list[tuple[str, UUID]],
    member_count: int,
    current_user_id: UUID | None,
) -> tuple[dict[str, object], bool, bool]:
    yes_count = 0
    no_count = 0
    active_vote: str | None = None
    for vote, voter_id in vote_rows:
        if vote == "yes":
            yes_count += 1
        elif vote == "no":
            no_count += 1
        if current_user_id is not None and voter_id == current_user_id:
            active_vote = vote

    total_votes = yes_count + no_count
    votes_required = required_votes(member_count)
    approval_percent = (yes_count / total_votes * 100.0) if total_votes > 0 else 0.0
    meets_quorum = total_votes >= votes_required
    passes = meets_quorum and approval_percent >= 66.0

    remaining_eligible = max(0, member_count - total_votes)
    max_yes = yes_count + remaining_eligible
    max_total = total_votes + remaining_eligible
    can_meet_quorum = max_total >= votes_required
    can_meet_approval = (max_yes / max_total * 100.0) >= 66.0 if max_total > 0 else False
    can_still_pass = (not passes) and can_meet_quorum and can_meet_approval

    quorum_threshold_percent = (votes_required / member_count * 100.0) if member_count > 0 else 0.0
    summary = {
        "yesCount": yes_count,
        "noCount": no_count,
        "totalVotes": total_votes,
        "approvalPercent": approval_percent,
        "activeVote": active_vote,
        "meetsQuorum": meets_quorum,
        "eligibleVoterCount": member_count,
        "quorumThresholdPercent": quorum_threshold_percent,
        "votesRequired": votes_required,
        "votesRemaining": max(0, votes_required - total_votes),
        "remainingEligibleVotes": remaining_eligible,
    }
    return summary, passes, can_still_pass


def _plan_leader_status(
    *,
    is_leading: bool,
    passes: bool,
    approval_percent: float,
    passing_plans: list[tuple[str, float]],
) -> str | None:
    if is_leading:
        return "leading"
    if not passes or not passing_plans:
        return None
    max_percent = max(percent for _, percent in passing_plans)
    top_count = sum(1 for _, percent in passing_plans if percent == max_percent)
    if approval_percent == max_percent and top_count > 1:
        return "tied"
    return None


def _lifecycle_phases(current_phase_id: str) -> list[dict[str, object]]:
    from app.services.lifecycle_copy import project_phase_copy

    phase_order = {phase_id: order for phase_id, order, _, _, _ in PROJECT_PHASES}
    current_order = phase_order.get(current_phase_id, 1)
    phases: list[dict[str, object]] = []
    for phase_id, order, short_label, title, summary in PROJECT_PHASES:
        if order < current_order:
            progress = "complete"
        elif order == current_order:
            progress = "current"
        else:
            progress = "upcoming"
        copy = project_phase_copy(phase_id, "productive", summary)
        phases.append(
            {
                "id": phase_id,
                "order": order,
                "shortLabel": short_label,
                "title": title,
                "summary": copy["summary"],
                "progressState": progress,
                "projectStatus": "active",
                "mechanics": copy["mechanics"],
                "note": copy["note"],
            }
        )
    return phases


def _visible_lifecycle_phases(
    project_mode: str,
    project_subtype: str | None,
    current_phase_id: str,
) -> list[dict[str, object]]:
    from app.services.lifecycle_copy import project_phase_copy
    from app.services.projects_phases import (
        effective_phase_id_for_progress,
        lifecycle_phase_title,
        visible_phase_ids_for_project,
    )

    phase_order = {phase_id: order for phase_id, order, _, _, _ in PROJECT_PHASES}
    phase_meta = {phase_id: (order, short_label, title, summary) for phase_id, order, short_label, title, summary in PROJECT_PHASES}
    current_order = phase_order.get(effective_phase_id_for_progress(current_phase_id), 1)
    visible_ids = visible_phase_ids_for_project(project_mode, project_subtype, current_phase_id)

    phases: list[dict[str, object]] = []
    for index, phase_id in enumerate(visible_ids, start=1):
        order, short_label, title, summary = phase_meta[phase_id]
        if order < current_order:
            progress = "complete"
        elif order == current_order:
            progress = "current"
        else:
            progress = "upcoming"
        copy = project_phase_copy(phase_id, project_mode, summary)
        phases.append(
            {
                "id": phase_id,
                "order": index,
                "shortLabel": short_label,
                "title": lifecycle_phase_title(project_mode, phase_id, title),
                "summary": copy["summary"],
                "progressState": progress,
                "projectStatus": "active",
                "mechanics": copy["mechanics"],
                "note": copy["note"],
            }
        )
    return phases


def _build_project_history(
    db: Session,
    project_id: UUID,
    current_user_id: UUID | None,
    vote_context_population: int,
) -> list[dict[str, object]]:
    phase_title_map = {item[0]: item[3] for item in PROJECT_PHASES}
    history: list[tuple[object, dict[str, object]]] = []
    from app.models import project_update_requests, project_update_request_votes, project_edit_requests, project_edit_request_votes, project_phase_change_requests, project_phase_change_votes

    def _author_username(author_id):
        if author_id is None:
            return "unknown"
        row = db.execute(select(users.c.username).where(users.c.id == author_id)).first()
        return row[0] if row else "unknown"

    update_rows = db.execute(
        select(project_update_requests)
        .where(project_update_requests.c.project_id == project_id)
        .order_by(project_update_requests.c.created_at.desc())
    ).mappings().all()
    for req in update_rows:
        vote_rows = db.execute(
            select(project_update_request_votes.c.vote, project_update_request_votes.c.voter_id)
            .where(project_update_request_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, vote_context_population, current_user_id)
        history.append((
            req["created_at"],
            {
                "id": str(req["id"]),
                "entityKind": "project",
                "kind": "project-update",
                "kindLabel": "Update decision",
                "createdAt": _iso(req["created_at"]),
                "authorUsername": _author_username(req["author_id"]),
                "status": req["status"],
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
                "canVote": req["status"] == "open",
                "payload": {"type": "update", "body": req["body"], "appliedUpdateId": None},
            },
        ))

    edit_rows = db.execute(
        select(project_edit_requests)
        .where(project_edit_requests.c.project_id == project_id)
        .order_by(project_edit_requests.c.created_at.desc())
    ).mappings().all()
    for req in edit_rows:
        vote_rows = db.execute(
            select(project_edit_request_votes.c.vote, project_edit_request_votes.c.voter_id)
            .where(project_edit_request_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, vote_context_population, current_user_id)
        history.append((
            req["created_at"],
            {
                "id": str(req["id"]),
                "entityKind": "project",
                "kind": "project-edit",
                "kindLabel": "Edit decision",
                "createdAt": _iso(req["created_at"]),
                "authorUsername": _author_username(req["author_id"]),
                "status": req["status"],
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
                "canVote": req["status"] == "open",
                "payload": {
                    "type": "edit",
                    "changes": [
                        {"label": "Title", "before": "", "after": req["title"]},
                        {"label": "Description", "before": "", "after": req["description"]},
                    ],
                },
            },
        ))

    phase_rows = db.execute(
        select(project_phase_change_requests)
        .where(project_phase_change_requests.c.project_id == project_id)
        .order_by(project_phase_change_requests.c.created_at.desc())
    ).mappings().all()
    for req in phase_rows:
        vote_rows = db.execute(
            select(project_phase_change_votes.c.vote, project_phase_change_votes.c.voter_id)
            .where(project_phase_change_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, vote_context_population, current_user_id)
        history.append((
            req["created_at"],
            {
                "id": str(req["id"]),
                "entityKind": "project",
                "kind": "project-phase-change",
                "kindLabel": "Phase decision",
                "createdAt": _iso(req["created_at"]),
                "authorUsername": _author_username(req["author_id"]),
                "status": req["status"],
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
                "canVote": req["status"] == "open",
                "payload": {
                    "type": "phase-change",
                    "changeKind": req["change_kind"],
                    "fromPhaseId": req["from_phase_id"],
                    "fromPhaseLabel": phase_title_map.get(req["from_phase_id"], req["from_phase_id"]),
                    "toPhaseId": req["target_phase_id"],
                    "toPhaseLabel": phase_title_map.get(req["target_phase_id"], req["target_phase_id"]),
                    "reason": req["reason"],
                },
            },
        ))

    project_subtype = db.execute(
        select(projects.c.project_subtype).where(projects.c.id == project_id)
    ).scalar_one_or_none()
    if project_subtype == "software":
        from app.services.projects_software import build_software_history_entries

        for created_at, entry in build_software_history_entries(
            db, project_id, vote_context_population, current_user_id
        ):
            history.append((created_at, entry))

    return [entry for _, entry in sorted(history, key=lambda item: item[0], reverse=True)]


