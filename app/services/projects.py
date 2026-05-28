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
    user_follows,
    users,
)
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.search import index_document
from app.services.projects_software import get_project_software_governance
from app.utils.votes import required_votes, resolve_project_vote_population

PROJECT_MODES = frozenset({"productive", "collective-service", "personal-service"})
PROJECT_SUBTYPES = frozenset({"standard", "software"})
PROJECT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
PROJECT_PHASES = (
    ("phase-1", 1, "P1", "Discovery", "Define values and demand."),
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


def _get_project_by_slug_row(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return row


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
    return "phase-1", "proposal"


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
    elif normalized_subtype not in PROJECT_SUBTYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"project_subtype must be one of: {sorted(PROJECT_SUBTYPES)} for non personal-service modes",
        )

    channel_ids = _resolve_channel_ids(db, channel_slugs)
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
                is_manager=False,
                is_manager_candidate=False,
                joined_at=now,
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
        select(users.c.id, users.c.username, users.c.bio).where(users.c.id.in_(list(user_ids)))
    ).all()
    return {
        row[0]: {
            "username": row[1],
            "bio": row[2] or "",
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


def _lifecycle_phases(current_phase_id: str) -> list[dict[str, object]]:
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
        phases.append(
            {
                "id": phase_id,
                "order": order,
                "shortLabel": short_label,
                "title": title,
                "summary": summary,
                "progressState": progress,
                "projectStatus": "active",
                "mechanics": [],
            }
        )
    return phases


async def get_project_detail(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
    cache: Redis | None = None,
) -> dict[str, object]:
    row = _get_project_by_slug_row(db, slug)
    project_id = row["id"]
    member_count = int(row["member_count"] or 0)
    vote_context_population = resolve_project_vote_population(
        db,
        project_id,
        bool(row["is_platform_tagged"]),
    )
    uses_platform_vote_context = bool(row["is_platform_tagged"])
    vote_context_label = "Weekly active platform users" if uses_platform_vote_context else "Weekly active project members"

    if cache is not None:
        try:
            signal_counts = await _get_signal_counts(db, cache, project_id)
        except Exception:
            signal_counts = _get_signal_counts_db(db, project_id)
    else:
        signal_counts = _get_signal_counts_db(db, project_id)

    viewer_membership = None
    if current_user_id is not None:
        viewer_membership = db.execute(
            select(project_memberships).where(
                project_memberships.c.project_id == project_id,
                project_memberships.c.user_id == current_user_id,
            )
        ).mappings().first()

    viewer_is_member = viewer_membership is not None
    viewer_is_manager = bool(viewer_membership["is_manager"]) if viewer_membership else False
    viewer_is_manager_candidate = bool(viewer_membership["is_manager_candidate"]) if viewer_membership else False

    author_row = db.execute(
        select(users.c.username).where(users.c.id == row["author_id"])
    ).first()
    author_username = author_row[0] if author_row else "unknown"

    channel_tag_rows = db.execute(
        select(channels.c.slug, channels.c.name)
        .select_from(project_tags.join(channels, channels.c.id == project_tags.c.channel_id))
        .where(project_tags.c.project_id == project_id, project_tags.c.tag_kind == "channel")
    ).all()
    channel_tags = [{"slug": slug_v, "label": name, "kind": "channel"} for slug_v, name in channel_tag_rows]

    community_tag_rows = db.execute(
        select(communities.c.slug, communities.c.name)
        .select_from(project_tags.join(communities, communities.c.id == project_tags.c.community_id))
        .where(project_tags.c.project_id == project_id, project_tags.c.tag_kind == "community")
    ).all()
    community_tags = [{"slug": slug_v, "label": name, "kind": "community"} for slug_v, name in community_tag_rows]

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "project",
                content_votes.c.target_id == project_id,
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    member_rows = db.execute(
        select(project_memberships.c.user_id, project_memberships.c.is_manager, project_memberships.c.is_manager_candidate)
        .where(project_memberships.c.project_id == project_id)
    ).all()
    member_ids = {member_id for member_id, _, _ in member_rows}

    usernames = _username_lookup(db, member_ids | ({row["author_id"]} if row["author_id"] else set()))

    members = []
    project_managers = []
    for member_id, is_manager, is_manager_candidate in member_rows:
        payload = {
            "id": str(member_id),
            "username": usernames.get(member_id, {}).get("username", "unknown"),
            "bio": usernames.get(member_id, {}).get("bio", ""),
            "isManagerCandidate": bool(is_manager_candidate),
        }
        members.append(payload)
        if is_manager:
            project_managers.append(payload)

    share_contact_rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio)
        .select_from(user_follows.join(users, users.c.id == user_follows.c.followed_id))
        .where(
            user_follows.c.follower_id == row["author_id"],
            user_follows.c.status == "accepted",
        )
        .limit(12)
    ).all()
    share_contacts = [
        {"id": str(user_id), "username": username, "bio": bio or ""}
        for user_id, username, bio in share_contact_rows
    ]

    value_rows = db.execute(
        select(project_values.c.id, project_values.c.label, project_values.c.author_id)
        .where(project_values.c.project_id == project_id)
        .order_by(project_values.c.created_at.asc())
    ).all()
    value_ids = [value_id for value_id, _, _ in value_rows]
    importance_rows = db.execute(
        select(
            project_value_importance_votes.c.value_id,
            project_value_importance_votes.c.voter_id,
            project_value_importance_votes.c.importance,
        ).where(project_value_importance_votes.c.value_id.in_(value_ids or [UUID(int=0)]))
    ).all() if value_ids else []

    votes_by_value: dict[UUID, list[tuple[UUID, int]]] = {}
    for value_id, voter_id, importance in importance_rows:
        votes_by_value.setdefault(value_id, []).append((voter_id, int(importance)))

    phase_one_values = []
    for value_id, label, value_author_id in value_rows:
        votes = votes_by_value.get(value_id, [])
        vote_count = len(votes)
        avg = (sum(score for _, score in votes) / vote_count) if vote_count > 0 else 0.0
        if avg >= 7:
            importance_label = "high"
        elif avg >= 4:
            importance_label = "medium"
        else:
            importance_label = "low"
        active_importance_vote = 0
        if current_user_id is not None:
            for voter_id, score in votes:
                if voter_id == current_user_id:
                    active_importance_vote = score
                    break
        phase_one_values.append(
            {
                "id": str(value_id),
                "label": label,
                "authorUsername": usernames.get(value_author_id, {}).get("username", "unknown"),
                "voteCount": vote_count,
                "importanceScore": round(avg, 2),
                "importanceLabel": importance_label,
                "activeImportanceVote": active_importance_vote,
            }
        )

    viewer_signal = None
    if current_user_id is not None:
        viewer_signal_row = db.execute(
            select(project_signals.c.signal_type).where(
                project_signals.c.project_id == project_id,
                project_signals.c.user_id == current_user_id,
            )
        ).first()
        if viewer_signal_row is not None:
            viewer_signal = viewer_signal_row[0]

    required_demand = required_votes(vote_context_population)
    signal_total = signal_counts["total"]
    demand_ratio_percent = (signal_counts["demand"] / signal_total * 100.0) if signal_total > 0 else 0.0
    signal_summary = {
        "demandCount": signal_counts["demand"],
        "oppositionCount": signal_counts["opposition"],
        "totalCount": signal_counts["total"],
        "viewerSignal": viewer_signal,
        "signalRatioPercent": demand_ratio_percent,
        "ratioRequirementMet": demand_ratio_percent >= 66.0 if signal_total > 0 else False,
        "requiredDemandCount": required_demand,
        "demandRequirementMet": signal_counts["demand"] >= required_demand,
        "advancementUnlocked": signal_counts["demand"] >= required_demand,
        "usesPlatformVoteContext": uses_platform_vote_context,
        "voteContextLabel": vote_context_label,
        "voteContextPopulation": vote_context_population,
    }

    plan_rows = db.execute(
        select(project_plans).where(project_plans.c.project_id == project_id).order_by(project_plans.c.created_at.desc())
    ).mappings().all()

    phase_two_plans = []
    phase_three_plans = []
    phase_two_winning = None
    phase_three_winning = None

    for plan in plan_rows:
        plan_vote_rows = db.execute(
            select(project_plan_votes.c.vote, project_plan_votes.c.voter_id).where(project_plan_votes.c.plan_id == plan["id"])
        ).all()
        overall_summary, _, _ = _vote_summary(plan_vote_rows, vote_context_population, current_user_id)

        value_assessments = []
        for value_id, value_label, _ in value_rows:
            value_vote_rows = db.execute(
                select(project_plan_value_votes.c.vote, project_plan_value_votes.c.voter_id)
                .where(
                    project_plan_value_votes.c.plan_id == plan["id"],
                    project_plan_value_votes.c.value_id == value_id,
                )
            ).all()
            summary, _, _ = _vote_summary(value_vote_rows, vote_context_population, current_user_id)
            value_assessments.append({
                "valueId": str(value_id),
                "valueLabel": value_label,
                **summary,
            })

        plan_payload = dict(plan["plan_payload"] or {})
        plan_phases = [
            {
                "id": str(item.get("id") or f"phase-{idx + 1}"),
                "title": str(item.get("title") or f"Phase {idx + 1}"),
                "details": str(item.get("details") or ""),
                "materialsLabel": str(item.get("materialsLabel") or ""),
                "costLabel": str(item.get("costLabel") or ""),
            }
            for idx, item in enumerate(list(plan_payload.get("planPhases") or []))
        ]

        base_plan = {
            "id": str(plan["id"]),
            "title": plan["title"],
            "authorUsername": usernames.get(plan["author_id"], {}).get("username", "unknown"),
            "createdAt": _iso(plan["created_at"]),
            "description": plan["description"],
            "repositoryUrl": plan["repository_url"],
            "demandSignalSnapshot": signal_counts["demand"],
            "demandConsiderationNote": plan["demand_consideration_note"] or "",
            "totalCostLabel": plan["total_cost_label"] or "",
            "planPhases": plan_phases,
            "valueAssessments": value_assessments,
            "overallApproval": overall_summary,
            "isLeading": bool(plan["is_leading"]),
        }

        if plan["phase_kind"] in {"production", "organisation"}:
            item = {
                **base_plan,
                "projectSubtype": plan["project_subtype"] or "standard",
                "projectSubtypeLabel": (plan["project_subtype"] or "standard").replace("-", " ").title(),
                "outputSummary": str(plan_payload.get("outputSummary") or ""),
                "materialsSummary": str(plan_payload.get("materialsSummary") or ""),
                "acquisitionsSummary": str(plan_payload.get("acquisitionsSummary") or ""),
                "acquisitionBundles": list(plan_payload.get("acquisitionBundles") or []),
                "purchaseRows": list(plan_payload.get("purchaseRows") or []),
                "viewerCanEdit": current_user_id is not None and plan["author_id"] == current_user_id,
            }
            phase_two_plans.append(item)
            if plan["is_leading"]:
                phase_two_winning = str(plan["id"])

        if plan["phase_kind"] in {"distribution", "access"}:
            item = {
                **base_plan,
                "distributionSummary": str(plan_payload.get("distributionSummary") or ""),
                "accessSummary": str(plan_payload.get("accessSummary") or ""),
                "reserveSummary": str(plan_payload.get("reserveSummary") or ""),
                "requestSystemEnabled": bool(plan_payload.get("requestSystemEnabled") or False),
                "requestMode": str(plan_payload.get("requestMode") or "both"),
                "allowOffScheduleRequests": bool(plan_payload.get("allowOffScheduleRequests") or False),
            }
            phase_three_plans.append(item)
            if plan["is_leading"]:
                phase_three_winning = str(plan["id"])

    activities_rows = db.execute(
        select(project_activities).where(project_activities.c.project_id == project_id).order_by(project_activities.c.scheduled_at.desc())
    ).mappings().all()
    activity_ids = [row_v["id"] for row_v in activities_rows]
    role_rows = db.execute(
        select(project_activity_roles).where(project_activity_roles.c.activity_id.in_(activity_ids or [UUID(int=0)]))
    ).mappings().all() if activity_ids else []

    role_ids = [role["id"] for role in role_rows]
    assignment_rows = db.execute(
        select(project_activity_assignments.c.role_id, project_activity_assignments.c.user_id)
        .where(project_activity_assignments.c.role_id.in_(role_ids or [UUID(int=0)]))
    ).all() if role_ids else []

    assignments_by_role: dict[UUID, list[UUID]] = {}
    for role_id, user_id in assignment_rows:
        assignments_by_role.setdefault(role_id, []).append(user_id)

    roles_by_activity: dict[UUID, list[Mapping[str, object]]] = {}
    for role in role_rows:
        roles_by_activity.setdefault(role["activity_id"], []).append(role)

    activities = []
    for activity in activities_rows:
        activity_roles = []
        minimum_participants = 0
        maximum_participants: int | None = 0
        committed_users: set[UUID] = set()
        viewer_assigned_label = None

        for role in roles_by_activity.get(activity["id"], []):
            assigned_users = assignments_by_role.get(role["id"], [])
            committed_users.update(assigned_users)
            is_viewer_assigned = current_user_id is not None and current_user_id in assigned_users
            if is_viewer_assigned:
                viewer_assigned_label = role["label"]

            minimum_participants += int(role["required_count"] or 0)
            if role["maximum_count"] is None:
                maximum_participants = None
            elif maximum_participants is not None:
                maximum_participants += int(role["maximum_count"])

            activity_roles.append(
                {
                    "label": role["label"],
                    "filledCount": len(assigned_users),
                    "requiredCount": int(role["required_count"] or 0),
                    "maximumCount": role["maximum_count"],
                    "isViewerAssigned": is_viewer_assigned,
                }
            )

        activities.append(
            {
                "id": str(activity["id"]),
                "title": activity["title"],
                "authorUsername": usernames.get(activity["author_id"], {}).get("username", "unknown"),
                "scheduledAt": _iso(activity["scheduled_at"]),
                "startAt": _iso(activity["scheduled_at"]),
                "endAt": _iso(activity["ends_at"]),
                "locationLabel": activity["location_label"],
                "minimumParticipants": minimum_participants,
                "maximumParticipants": maximum_participants,
                "committedCount": len(committed_users),
                "viewerAssignedRoleLabel": viewer_assigned_label,
                "linkedPlanPhaseLabel": activity["linked_plan_phase_id"],
                "statusTone": "green" if activity["status"] == "active" else "yellow",
                "roles": activity_roles,
                "note": activity["note"],
                "isActive": activity["status"] == "active",
            }
        )

    updates_rows = db.execute(
        select(project_updates).where(project_updates.c.project_id == project_id).order_by(project_updates.c.created_at.desc())
    ).mappings().all()
    updates = [
        {
            "id": str(item["id"]),
            "title": item["title"],
            "body": item["body"],
            "authorUsername": usernames.get(item["author_id"], {}).get("username", "unknown"),
            "createdAt": _iso(item["created_at"]),
        }
        for item in updates_rows
    ]

    def _build_request_list(request_table, vote_table, body_keys: list[str]) -> list[dict[str, object]]:
        rows = db.execute(
            select(request_table).where(request_table.c.project_id == project_id).order_by(request_table.c.created_at.desc())
        ).mappings().all()
        out = []
        for req in rows:
            vote_rows = db.execute(
                select(vote_table.c.vote, vote_table.c.voter_id).where(vote_table.c.request_id == req["id"])
            ).all()
            summary, passes, can_still = _vote_summary(vote_rows, vote_context_population, current_user_id)
            payload = {
                "id": str(req["id"]),
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
            for key in body_keys:
                payload[key] = req[key.lower()] if key.lower() in req else req.get(key)
            out.append(payload)
        return out

    update_requests = _build_request_list(project_update_requests, project_update_request_votes, ["body"])
    edit_requests = _build_request_list(project_edit_requests, project_edit_request_votes, ["title", "description"])

    phase_change_rows = db.execute(
        select(project_phase_change_requests)
        .where(project_phase_change_requests.c.project_id == project_id)
        .order_by(project_phase_change_requests.c.created_at.desc())
    ).mappings().all()
    phase_title_map = {item[0]: item[3] for item in PROJECT_PHASES}
    phase_change_requests = []
    for req in phase_change_rows:
        vote_rows = db.execute(
            select(project_phase_change_votes.c.vote, project_phase_change_votes.c.voter_id)
            .where(project_phase_change_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, vote_context_population, current_user_id)
        conversion_target = None
        if req["conversion_target_mode"] and req["conversion_target_subtype"]:
            conversion_target = {
                "projectMode": req["conversion_target_mode"],
                "projectSubtype": req["conversion_target_subtype"],
                "projectModeLabel": str(req["conversion_target_mode"]).replace("-", " ").title(),
                "projectSubtypeLabel": str(req["conversion_target_subtype"]).replace("-", " ").title(),
                "entryPhaseId": req["target_phase_id"],
                "entryPhaseLabel": phase_title_map.get(req["target_phase_id"], req["target_phase_id"]),
            }
        phase_change_requests.append(
            {
                "id": str(req["id"]),
                "targetPhaseId": req["target_phase_id"],
                "targetPhaseLabel": phase_title_map.get(req["target_phase_id"], req["target_phase_id"]),
                "reason": req["reason"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "kind": req["change_kind"],
                "closeOutcome": req["close_outcome"],
                "conversionTarget": conversion_target,
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    revert_rows = db.execute(
        select(project_revert_history).where(project_revert_history.c.project_id == project_id).order_by(project_revert_history.c.created_at.desc())
    ).mappings().all()
    revert_history = [
        {
            "id": str(item["id"]),
            "targetPhaseId": item["target_phase_id"],
            "reason": item["reason"],
            "authorUsername": usernames.get(item["author_id"], {}).get("username", "unknown"),
            "createdAt": _iso(item["created_at"]),
        }
        for item in revert_rows
    ]

    service_settings = db.execute(
        select(project_service_request_settings).where(project_service_request_settings.c.project_id == project_id)
    ).mappings().first()
    if service_settings is None:
        service_settings_payload = {
            "enabled": False,
            "requestMode": "both",
            "allowOffScheduleRequests": False,
            "summary": "",
        }
    else:
        service_settings_payload = {
            "enabled": bool(service_settings["enabled"]),
            "requestMode": service_settings["request_mode"],
            "allowOffScheduleRequests": bool(service_settings["allow_off_schedule_requests"]),
            "summary": service_settings["summary"],
        }

    service_requests_rows = db.execute(
        select(project_service_requests).where(project_service_requests.c.project_id == project_id).order_by(project_service_requests.c.created_at.desc())
    ).mappings().all()
    service_requests = [
        {
            "id": str(item["id"]),
            "title": item["title"],
            "body": item["body"],
            "requesterUsername": usernames.get(item["requester_id"], {}).get("username", "unknown"),
            "createdAt": _iso(item["created_at"]),
            "status": item["status"],
            "scheduledAt": _iso(item["scheduled_at"]) if item["scheduled_at"] else None,
            "endsAt": _iso(item["ends_at"]) if item["ends_at"] else None,
            "linkedActivityId": str(item["linked_activity_id"]) if item["linked_activity_id"] else None,
        }
        for item in service_requests_rows
    ]

    settings_change_rows = db.execute(
        select(project_service_request_setting_changes)
        .where(project_service_request_setting_changes.c.project_id == project_id)
        .order_by(project_service_request_setting_changes.c.created_at.desc())
    ).mappings().all()
    settings_change_requests = []
    for req in settings_change_rows:
        vote_rows = db.execute(
            select(project_service_request_setting_change_votes.c.vote, project_service_request_setting_change_votes.c.voter_id)
            .where(project_service_request_setting_change_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, vote_context_population, current_user_id)
        settings_change_requests.append(
            {
                "id": str(req["id"]),
                "reason": req["reason"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "proposedSettings": {
                    "enabled": bool(req["enabled"]),
                    "requestMode": req["request_mode"],
                    "allowOffScheduleRequests": bool(req["allow_off_schedule_requests"]),
                    "summary": service_settings_payload["summary"],
                },
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    link_rows = db.execute(
        select(project_links).where(project_links.c.source_project_id == project_id)
    ).mappings().all()
    auto_links = [
        {
            "id": str(item["id"]),
            "title": item["relationship_label"],
            "relationshipLabel": item["relationship_label"],
            "summary": item["summary"],
            "href": None,
            "publicItem": None,
        }
        for item in link_rows
    ]

    link_request_rows = db.execute(
        select(project_link_requests).where(project_link_requests.c.source_project_id == project_id).order_by(project_link_requests.c.created_at.desc())
    ).mappings().all()
    manual_link_requests = []
    for req in link_request_rows:
        this_votes = db.execute(
            select(project_link_request_votes.c.vote).where(
                project_link_request_votes.c.request_id == req["id"],
                project_link_request_votes.c.vote_scope == "source",
            )
        ).all()
        yes = sum(1 for (vote,) in this_votes if vote == "yes")
        no = sum(1 for (vote,) in this_votes if vote == "no")
        manual_link_requests.append(
            {
                "id": str(req["id"]),
                "title": req["relationship_label"],
                "relationshipLabel": req["relationship_label"],
                "summary": req["summary"],
                "statusLabel": req["status"],
                "proposedByUsername": usernames.get(req["proposed_by"], {}).get("username", "unknown"),
                "createdAtLabel": _iso(req["created_at"]),
                "targetProjectHref": None,
                "thisProjectVote": {
                    "projectTitle": row["title"],
                    "yesCount": yes,
                    "noCount": no,
                    "memberCount": member_count,
                    "approvalsRequired": required_votes(vote_context_population),
                    "approvalsRemaining": max(0, required_votes(vote_context_population) - yes),
                    "approvalPercent": (yes / (yes + no) * 100.0) if (yes + no) > 0 else 0.0,
                    "statusLabel": req["status"],
                    "resultNote": "",
                    "viewerCanVote": viewer_is_member,
                    "viewerVote": None,
                },
                "otherProjectVote": {
                    "projectTitle": "Linked project",
                    "yesCount": 0,
                    "noCount": 0,
                    "memberCount": 0,
                    "approvalsRequired": 0,
                    "approvalsRemaining": 0,
                    "approvalPercent": 0,
                    "statusLabel": "pending",
                    "resultNote": "",
                    "viewerCanVote": False,
                    "viewerVote": None,
                },
            }
        )

    linkable_rows = db.execute(
        select(projects.c.slug, projects.c.title).where(projects.c.id != project_id).order_by(projects.c.title.asc()).limit(20)
    ).all()
    linkable_projects = [{"slug": s, "title": t, "href": f"/projects/{s}"} for s, t in linkable_rows]

    phase_order = {phase_id: order for phase_id, order, _, _, _ in PROJECT_PHASES}
    current_order = phase_order.get(row["current_phase_id"], 1)
    next_phase = next((p for p in PROJECT_PHASES if p[1] == current_order + 1), None)

    software_governance = None
    if row["project_subtype"] == "software":
        software_governance = get_project_software_governance(db=db, project_slug=row["slug"], current_user_id=current_user_id)

    request_system = {
        "enabled": bool(service_settings_payload["enabled"]),
        "requestCount": len(service_requests),
        "requests": service_requests,
        "viewerCanSubmitRequests": viewer_is_member,
        "viewerCanReviewRequests": viewer_is_manager,
        "viewerCanRequestSettingsChanges": viewer_is_member,
        "viewerCanVoteOnSettingsChanges": viewer_is_member,
        "requiresSchedule": service_settings_payload["requestMode"] == "calendar",
        "settings": service_settings_payload,
        "settingsChangeRequests": settings_change_requests,
    }

    lifecycle = {
        "projectMode": row["project_mode"],
        "currentSubtype": row["project_subtype"],
        "currentSubtypeLabel": row["project_subtype"].replace("-", " ").title() if row["project_subtype"] else None,
        "usesPlatformLifecycle": row["project_mode"] != "personal-service",
        "supportsDemandSignals": True,
        "supportsPlanning": row["project_mode"] != "personal-service",
        "currentPhaseId": row["current_phase_id"],
        "quorumThresholdPercent": (required_votes(vote_context_population) / vote_context_population * 100.0) if vote_context_population > 0 else 0.0,
        "quorumVotesRequired": required_votes(vote_context_population),
        "voteContextLabel": vote_context_label,
        "voteContextPopulation": vote_context_population,
        "notes": [],
        "phases": _lifecycle_phases(row["current_phase_id"]),
        "viewerCanRequestPhaseChanges": viewer_is_member,
        "viewerCanVoteOnPhaseChanges": viewer_is_member,
        "phaseChangeRequests": phase_change_requests,
        "viewerCanAdvancePhase": viewer_is_manager,
        "nextPhaseId": next_phase[0] if next_phase else None,
        "nextPhaseLabel": next_phase[3] if next_phase else None,
        "viewerCanRevertPhase": viewer_is_manager,
        "revertablePhaseIds": [phase_id for phase_id, order, _, _, _ in PROJECT_PHASES if order < current_order and order <= 3],
        "revertHistory": revert_history,
        "requestSystem": request_system,
        "personalService": {
            "availabilitySummary": "",
            "travelRadiusLabel": "",
            "usesCalendar": service_settings_payload["requestMode"] == "calendar",
            "requestMode": service_settings_payload["requestMode"],
        } if row["project_mode"] == "personal-service" else None,
        "phaseOne": {
            "values": phase_one_values,
            "viewerCanSignalDemand": viewer_is_member,
            "viewerHasDemandSignal": viewer_signal == "demand",
            "viewerCanSignalOpposition": viewer_is_member,
            "viewerHasOppositionSignal": viewer_signal == "opposition",
            "signalSummary": signal_summary,
            "viewerCanAddValue": viewer_is_member,
            "viewerCanVoteOnValues": viewer_is_member,
        },
        "phaseTwo": {
            "plans": phase_two_plans,
            "winningPlanId": phase_two_winning,
            "viewerCanSubmitPlans": viewer_is_member,
            "viewerCanVoteOnPlans": viewer_is_member,
            "availableAssetManagementServices": [],
        },
        "phaseThree": {
            "plans": phase_three_plans,
            "winningPlanId": phase_three_winning,
            "viewerCanSubmitPlans": viewer_is_member,
            "viewerCanVoteOnPlans": viewer_is_member,
            "requestSystemEnabled": bool(service_settings_payload["enabled"]),
        },
        "phaseFour": None,
        "phaseFive": {
            "activities": activities,
            "history": [],
            "viewerCanCreateActivities": viewer_is_member,
            "selectablePlanPhases": [],
            "softwareGovernance": software_governance,
        },
    }

    report_row = db.execute(
        select(reports.c.id, reports.c.resolution).where(reports.c.target_type == "project", reports.c.target_id == project_id)
    ).first()
    report = None
    is_removed = False
    if report_row is not None:
        is_removed = report_row[1] == "removed"

    discussion_rows = db.execute(
        select(comments.c.id, comments.c.author_id, comments.c.body, comments.c.created_at, comments.c.vote_count)
        .where(comments.c.subject_type == "project", comments.c.subject_id == project_id, comments.c.parent_id.is_(None))
        .order_by(comments.c.created_at.asc())
    ).all()
    discussion = [
        {
            "id": str(comment_id),
            "authorUsername": usernames.get(author_id, {}).get("username", "unknown"),
            "body": body,
            "createdAt": _iso(created_at),
            "voteCount": int(vote_count or 0),
            "activeVote": 0,
            "report": None,
            "replies": [],
        }
        for comment_id, author_id, body, created_at, vote_count in discussion_rows
    ]

    return {
        "id": str(project_id),
        "slug": row["slug"],
        "createdAt": _iso(row["created_at"]),
        "title": row["title"],
        "authorUsername": author_username,
        "projectMode": row["project_mode"],
        "projectSubtype": row["project_subtype"],
        "description": row["description"],
        "channelTags": channel_tags,
        "communityTags": community_tags,
        "stage": row["stage_label"],
        "locationLabel": row["location_label"],
        "voteCount": int(row["vote_count"] or 0),
        "activeVote": active_vote,
        "signalCount": signal_counts["total"],
        "commentCount": int(row["comment_count"] or 0),
        "memberCount": member_count,
        "lastActivityAt": _iso(row["last_activity_at"]),
        "lifecycle": lifecycle,
        "updates": updates,
        "updateRequests": update_requests,
        "viewerCanRequestUpdate": viewer_is_member,
        "viewerCanVoteOnUpdateRequests": viewer_is_member,
        "editRequests": edit_requests,
        "viewerCanRequestEdit": viewer_is_member,
        "viewerCanVoteOnEditRequests": viewer_is_member,
        "linksFrame": {
            "projectSlug": row["slug"],
            "intro": "Project links",
            "autoLinks": auto_links,
            "manualLinks": [],
            "manualLinkRequests": manual_link_requests,
            "linkableProjects": linkable_projects,
            "viewerCanProposeLinks": viewer_is_member,
            "conversionNote": "",
            "conversionWorkflow": [],
            "conversionLineage": None,
            "requestFrames": [
                {"id": "borrowing", "title": "Borrowing", "body": ""},
                {"id": "delivery", "title": "Delivery", "body": ""},
                {"id": "asset-use", "title": "Asset use", "body": ""},
            ],
            "placeholderSections": [],
        },
        "inventoryFrame": None,
        "history": [],
        "projectManagers": project_managers,
        "members": members,
        "viewerIsMember": viewer_is_member,
        "viewerCanToggleMembership": current_user_id is not None,
        "viewerCanShare": viewer_is_member,
        "viewerCanToggleManagerNomination": viewer_is_member,
        "viewerIsManagerCandidate": viewer_is_manager_candidate,
        "viewerIsProjectManager": viewer_is_manager,
        "shareContacts": share_contacts,
        "report": report,
        "isRemovedByReport": is_removed,
        "discussionNote": "",
        "discussion": discussion,
    }


def join_project(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)

    inserted = False
    try:
        db.execute(
            insert(project_memberships).values(
                project_id=project_row["id"],
                user_id=current_user_id,
                is_manager=False,
                is_manager_candidate=False,
                joined_at=datetime.now(timezone.utc),
            )
        )
        inserted = True
    except IntegrityError:
        db.rollback()

    if inserted:
        try:
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(member_count=projects.c.member_count + 1)
            )
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not join project") from exc

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="join-project",
            metadata={"project_id": str(project_row["id"]), "project_slug": project_row["slug"]},
        )
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not join project") from exc

    return {"ok": True, "joined": True, "slug": project_row["slug"]}


def leave_project(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)

    try:
        result = db.execute(
            delete(project_memberships).where(
                project_memberships.c.project_id == project_row["id"],
                project_memberships.c.user_id == current_user_id,
            )
        )

        if result.rowcount and result.rowcount > 0:
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(member_count=func.greatest(projects.c.member_count - 1, 0))
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not leave project") from exc

    return {"ok": True, "joined": False, "slug": project_row["slug"]}


def _ensure_project_member(db: Session, project_id: UUID, user_id: UUID) -> None:
    membership = db.execute(
        select(project_memberships.c.user_id).where(
            project_memberships.c.project_id == project_id,
            project_memberships.c.user_id == user_id,
        )
    ).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only project members can perform this action")


def add_project_value(
    db: Session,
    current_user_id: UUID,
    slug: str,
    label: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    _ensure_project_member(db, project_row["id"], current_user_id)

    normalized = label.strip()
    if not normalized:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="label is required")

    try:
        created = db.execute(
            insert(project_values)
            .values(project_id=project_row["id"], label=normalized, author_id=current_user_id)
            .returning(project_values.c.id, project_values.c.project_id, project_values.c.label, project_values.c.author_id, project_values.c.created_at)
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not add project value") from exc

    return {
        "value": {
            "id": created["id"],
            "project_id": created["project_id"],
            "label": created["label"],
            "author_id": created["author_id"],
            "created_at": created["created_at"],
        }
    }


def vote_project_value_importance(
    db: Session,
    current_user_id: UUID,
    slug: str,
    value_id: UUID,
    importance: int,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    _ensure_project_member(db, project_row["id"], current_user_id)

    if importance < 1 or importance > 10:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="importance must be between 1 and 10")

    value_row = db.execute(
        select(project_values).where(
            project_values.c.id == value_id,
            project_values.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if value_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project value not found")

    existing = db.execute(
        select(project_value_importance_votes.c.importance).where(
            project_value_importance_votes.c.value_id == value_id,
            project_value_importance_votes.c.voter_id == current_user_id,
        )
    ).first()

    try:
        if existing is None:
            db.execute(
                insert(project_value_importance_votes).values(
                    value_id=value_id,
                    voter_id=current_user_id,
                    importance=importance,
                )
            )
        else:
            db.execute(
                update(project_value_importance_votes)
                .where(
                    project_value_importance_votes.c.value_id == value_id,
                    project_value_importance_votes.c.voter_id == current_user_id,
                )
                .values(importance=importance)
            )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not vote on project value") from exc

    record_meaningful_action(
        db=db,
        user_id=current_user_id,
        action_type="cast-vote",
        metadata={
            "target_type": "project-value",
            "target_id": str(value_id),
            "project_id": str(project_row["id"]),
            "importance": importance,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not vote on project value") from exc

    return {
        "ok": True,
        "project_slug": project_row["slug"],
        "value_id": value_id,
        "importance": importance,
    }


def create_project_activity(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    scheduled_at: datetime,
    ends_at: datetime,
    location_label: str,
    note: str,
    role_requirements: list[dict[str, object]],
    linked_plan_id: UUID | None = None,
    linked_plan_phase_id: str | None = None,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    _ensure_project_member(db, project_row["id"], current_user_id)

    if ends_at <= scheduled_at:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ends_at must be after scheduled_at")

    try:
        created = db.execute(
            insert(project_activities)
            .values(
                project_id=project_row["id"],
                linked_plan_id=linked_plan_id,
                linked_plan_phase_id=linked_plan_phase_id,
                linked_request_id=None,
                title=title.strip(),
                author_id=current_user_id,
                scheduled_at=scheduled_at,
                ends_at=ends_at,
                location_label=location_label.strip(),
                note=note.strip(),
                status="active",
            )
            .returning(
                project_activities.c.id,
                project_activities.c.project_id,
                project_activities.c.title,
                project_activities.c.author_id,
                project_activities.c.scheduled_at,
                project_activities.c.ends_at,
                project_activities.c.location_label,
                project_activities.c.note,
                project_activities.c.linked_plan_id,
                project_activities.c.linked_plan_phase_id,
                project_activities.c.status,
                project_activities.c.created_at,
            )
        ).mappings().one()

        role_items = []
        for req in role_requirements:
            label = str(req.get("label", "")).strip()
            required_count = int(req.get("required_count", 0))
            maximum_count_raw = req.get("maximum_count")
            maximum_count = int(maximum_count_raw) if maximum_count_raw is not None else None
            if not label or required_count < 1:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid role requirement")
            if maximum_count is not None and maximum_count < required_count:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="maximum_count must be >= required_count")

            role = db.execute(
                insert(project_activity_roles)
                .values(
                    activity_id=created["id"],
                    label=label,
                    required_count=required_count,
                    maximum_count=maximum_count,
                )
                .returning(
                    project_activity_roles.c.id,
                    project_activity_roles.c.label,
                    project_activity_roles.c.required_count,
                    project_activity_roles.c.maximum_count,
                )
            ).mappings().one()
            role_items.append(dict(role))

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create project activity") from exc
    except HTTPException:
        db.rollback()
        raise
    return {
        "activity": {
            **dict(created),
            "roles": role_items,
        }
    }


def commit_project_activity_role(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    role_label: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    _ensure_project_member(db, project_row["id"], current_user_id)

    activity_row = db.execute(
        select(project_activities).where(
            project_activities.c.id == activity_id,
            project_activities.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if activity_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")

    role_row = db.execute(
        select(project_activity_roles).where(
            project_activity_roles.c.activity_id == activity_id,
            project_activity_roles.c.label == role_label.strip(),
        )
    ).mappings().first()
    if role_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    existing_assignment = db.execute(
        select(project_activity_assignments.c.role_id)
        .select_from(project_activity_assignments.join(project_activity_roles, project_activity_roles.c.id == project_activity_assignments.c.role_id))
        .where(
            project_activity_roles.c.activity_id == activity_id,
            project_activity_assignments.c.user_id == current_user_id,
        )
    ).first()
    if existing_assignment is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already assigned in this activity")

    filled_count = db.execute(
        select(project_activity_assignments.c.user_id).where(project_activity_assignments.c.role_id == role_row["id"])
    ).all()
    if role_row["maximum_count"] is not None and len(filled_count) >= int(role_row["maximum_count"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role is already full")

    try:
        db.execute(
            insert(project_activity_assignments).values(role_id=role_row["id"], user_id=current_user_id)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not commit activity role") from exc

    return {"ok": True, "project_slug": project_row["slug"], "activity_id": activity_id, "role_id": role_row["id"], "user_id": current_user_id}


def uncommit_project_activity_role(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    _ensure_project_member(db, project_row["id"], current_user_id)

    role_ids = db.execute(
        select(project_activity_roles.c.id).where(project_activity_roles.c.activity_id == activity_id)
    ).scalars().all()

    if role_ids:
        db.execute(
            delete(project_activity_assignments).where(
                project_activity_assignments.c.role_id.in_(role_ids),
                project_activity_assignments.c.user_id == current_user_id,
            )
        )
        db.commit()

    return {"ok": True, "project_slug": project_row["slug"], "activity_id": activity_id}


def add_project_update(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    body: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    _ensure_project_member(db, project_row["id"], current_user_id)

    normalized_title = title.strip()
    normalized_body = body.strip()
    if not normalized_title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title is required")
    if not normalized_body:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="body is required")

    created = db.execute(
        insert(project_updates)
        .values(
            project_id=project_row["id"],
            title=normalized_title,
            body=normalized_body,
            author_id=current_user_id,
        )
        .returning(
            project_updates.c.id,
            project_updates.c.project_id,
            project_updates.c.title,
            project_updates.c.body,
            project_updates.c.author_id,
            project_updates.c.created_at,
        )
    ).mappings().one()
    db.commit()

    return {
        "update": {
            "id": created["id"],
            "project_id": created["project_id"],
            "title": created["title"],
            "body": created["body"],
            "author_id": created["author_id"],
            "created_at": created["created_at"],
        }
    }


def share_project_with_user(
    db: Session,
    current_user_id: UUID,
    slug: str,
    username: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    _ensure_project_member(db, project_row["id"], current_user_id)

    normalized_username = username.strip()
    if not normalized_username:
        return {"ok": False, "error": "Choose another user."}

    target_user = db.execute(
        select(users.c.id, users.c.username).where(users.c.username == normalized_username)
    ).mappings().first()
    if target_user is None or target_user["id"] == current_user_id:
        return {"ok": False, "error": "Choose another user."}

    create_notification(
        db=db,
        recipient_id=target_user["id"],
        actor_id=current_user_id,
        kind="prj-share",
        surface="project",
        subject_type="project",
        subject_id=project_row["id"],
        target_id=project_row["id"],
        title=project_row["title"],
        body="A project was shared with you.",
        href=f"/projects/{project_row['slug']}",
    )
    return {"ok": True}


async def toggle_project_signal(
    db: Session,
    cache: Redis,
    current_user_id: UUID,
    slug: str,
    signal_type: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    normalized_signal = signal_type.strip().lower()

    if normalized_signal not in PROJECT_SIGNAL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"signal_type must be one of: {sorted(PROJECT_SIGNAL_TYPES)}",
        )

    existing = db.execute(
        select(project_signals.c.id, project_signals.c.signal_type)
        .where(
            project_signals.c.project_id == project_row["id"],
            project_signals.c.user_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    action = "none"
    signal_count_delta = 0

    try:
        if existing is None:
            db.execute(
                insert(project_signals).values(
                    project_id=project_row["id"],
                    user_id=current_user_id,
                    signal_type=normalized_signal,
                )
            )
            signal_count_delta = 1
            action = "added"
        elif existing["signal_type"] == normalized_signal:
            db.execute(delete(project_signals).where(project_signals.c.id == existing["id"]))
            signal_count_delta = -1
            action = "removed"
        else:
            db.execute(
                update(project_signals)
                .where(project_signals.c.id == existing["id"])
                .values(signal_type=normalized_signal)
            )
            action = "switched"

        if signal_count_delta != 0:
            db.execute(
                update(projects)
                .where(projects.c.id == project_row["id"])
                .values(signal_count=func.greatest(projects.c.signal_count + signal_count_delta, 0))
            )

        if normalized_signal == "demand" and action in {"added", "switched"}:
            record_meaningful_action(
                db=db,
                user_id=current_user_id,
                action_type="signal-demand",
                metadata={"project_id": str(project_row["id"]), "project_slug": project_row["slug"]},
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not toggle signal") from exc

    counts = _get_signal_counts_db(db, project_row["id"])
    await _write_signal_counts_cache(cache, project_row["id"], counts)

    return {
        "ok": True,
        "slug": project_row["slug"],
        "action": action,
        "signal_type": normalized_signal,
        "signals": counts,
    }
