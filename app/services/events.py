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
    event_activity_assignments,
    event_activity_roles,
    event_activities,
    event_attendance,
    event_edit_request_votes,
    event_edit_requests,
    event_editors,
    event_memberships,
    event_phase_change_requests,
    event_phase_change_votes,
    event_plan_value_votes,
    event_plan_votes,
    event_plans,
    event_signals,
    event_tags,
    event_update_request_votes,
    event_update_requests,
    event_updates,
    event_value_importance_votes,
    event_values,
    events,
    reports,
    user_follows,
    users,
)
from app.services.search import index_document
from app.utils.votes import required_votes

EVENT_SIGNAL_TYPES = frozenset({"demand", "opposition"})
EVENT_ATTENDANCE_STATES = frozenset({"going", "not-going"})
EVENT_PHASES = (
    ("proposal", 1, "P1", "Proposal", "Collect demand and define event values."),
    ("event-plan", 2, "P2", "Event Plan", "Propose and approve event plans."),
    ("activity", 3, "P3", "Activity", "Run event activities."),
    ("closed", 4, "P4", "Closed", "Event is closed."),
)


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
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


def _event_lifecycle_phases(current_phase_id: str) -> list[dict[str, object]]:
    phase_order = {phase_id: order for phase_id, order, _, _, _ in EVENT_PHASES}
    current_order = phase_order.get(current_phase_id, 1)
    phases: list[dict[str, object]] = []
    for phase_id, order, short_label, title, summary in EVENT_PHASES:
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
                "eventStatus": "active",
                "mechanics": [],
            }
        )
    return phases


def _serialize_event(
    row: Mapping[str, object],
    tags: list[dict[str, object]],
    signal_counts: dict[str, int],
) -> dict[str, object]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "description": row["description"],
        "created_by": row["created_by"],
        "is_private": row["is_private"],
        "current_phase_id": row["current_phase_id"],
        "time_label": row["time_label"],
        "location_label": row["location_label"],
        "scheduled_at": row["scheduled_at"],
        "signal_count": signal_counts["total"],
        "vote_count": row["vote_count"],
        "comment_count": row["comment_count"],
        "going_count": row["going_count"],
        "member_count": row["member_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_activity_at": row["last_activity_at"],
        "tags": tags,
        "signals": signal_counts,
    }


def _get_event_by_slug_row(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(events).where(events.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
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


def _get_event_tags(db: Session, event_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            event_tags.c.id,
            event_tags.c.tag_kind,
            event_tags.c.channel_id,
            event_tags.c.community_id,
        ).where(event_tags.c.event_id == event_id)
    ).mappings().all()
    return [dict(row) for row in rows]


def _get_signal_counts_db(db: Session, event_id: UUID) -> dict[str, int]:
    grouped_rows = db.execute(
        select(event_signals.c.signal_type, func.count().label("count"))
        .where(event_signals.c.event_id == event_id)
        .group_by(event_signals.c.signal_type)
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


async def _write_signal_counts_cache(cache: Redis, event_id: UUID, counts: dict[str, int]) -> None:
    key = f"event:{event_id}:signals"
    await cache.hset(
        key,
        mapping={
            "demand": str(counts["demand"]),
            "opposition": str(counts["opposition"]),
            "total": str(counts["total"]),
        },
    )


async def _get_signal_counts(db: Session, cache: Redis, event_id: UUID) -> dict[str, int]:
    key = f"event:{event_id}:signals"
    cached = await cache.hgetall(key)
    if cached:
        return {
            "demand": int(cached.get("demand", 0)),
            "opposition": int(cached.get("opposition", 0)),
            "total": int(cached.get("total", 0)),
        }

    counts = _get_signal_counts_db(db, event_id)
    await _write_signal_counts_cache(cache, event_id, counts)
    return counts


def create_event(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    description: str,
    is_private: bool,
    time_label: str,
    location_label: str,
    channel_slugs: list[str],
    scheduled_at: datetime | None = None,
) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    if not is_private and not channel_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Public events require at least one channel tag",
        )

    now = datetime.now(timezone.utc)

    try:
        created = db.execute(
            insert(events)
            .values(
                slug=normalized_slug,
                title=title.strip(),
                description=description.strip(),
                created_by=current_user_id,
                is_private=is_private,
                current_phase_id="phase-1",
                time_label=time_label.strip(),
                location_label=location_label.strip(),
                scheduled_at=scheduled_at,
                member_count=1,
                last_activity_at=now,
            )
            .returning(
                events.c.id,
                events.c.slug,
                events.c.title,
                events.c.description,
                events.c.created_by,
                events.c.is_private,
                events.c.current_phase_id,
                events.c.time_label,
                events.c.location_label,
                events.c.scheduled_at,
                events.c.vote_count,
                events.c.comment_count,
                events.c.going_count,
                events.c.member_count,
                events.c.created_at,
                events.c.updated_at,
                events.c.last_activity_at,
            )
        ).mappings().one()

        db.execute(
            insert(event_memberships).values(
                event_id=created["id"],
                user_id=current_user_id,
                role="member",
                joined_at=now,
            )
        )

        for channel_id in channel_ids:
            db.execute(
                insert(event_tags).values(
                    event_id=created["id"],
                    tag_kind="channel",
                    channel_id=channel_id,
                    community_id=None,
                )
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Event slug already exists") from exc

    tags = _get_event_tags(db, created["id"])
    index_document(
        db=db,
        entity_type="event",
        entity_id=created["id"],
        title=created["title"],
        summary=created["description"],
        meta=created["location_label"],
        href=f"/events/{created['slug']}",
    )
    return {"event": _serialize_event(created, tags, {"demand": 0, "opposition": 0, "total": 0})}


async def get_event_by_slug(db: Session, cache: Redis, slug: str) -> dict[str, object]:
    row = _get_event_by_slug_row(db, slug)
    tags = _get_event_tags(db, row["id"])
    signal_counts = await _get_signal_counts(db, cache, row["id"])
    return {"event": _serialize_event(row, tags, signal_counts)}


async def get_event_detail(
    db: Session,
    slug: str,
    current_user_id: UUID | None = None,
    cache: Redis | None = None,
) -> dict[str, object]:
    row = _get_event_by_slug_row(db, slug)
    event_id = row["id"]
    member_count = int(row["member_count"] or 0)

    if cache is not None:
        try:
            signal_counts = await _get_signal_counts(db, cache, event_id)
        except Exception:
            signal_counts = _get_signal_counts_db(db, event_id)
    else:
        signal_counts = _get_signal_counts_db(db, event_id)

    membership_rows = db.execute(
        select(event_memberships.c.user_id, event_memberships.c.role).where(event_memberships.c.event_id == event_id)
    ).all()
    member_ids = {user_id for user_id, _ in membership_rows}
    usernames = _username_lookup(db, member_ids | ({row["created_by"]} if row["created_by"] else set()))

    viewer_is_member = current_user_id is not None and current_user_id in member_ids

    editor_rows = db.execute(
        select(event_editors.c.user_id).where(event_editors.c.event_id == event_id)
    ).all()
    editor_ids = {user_id for (user_id,) in editor_rows}

    creator_username = usernames.get(row["created_by"], {}).get("username", "unknown")

    channel_tag_rows = db.execute(
        select(channels.c.slug, channels.c.name)
        .select_from(event_tags.join(channels, channels.c.id == event_tags.c.channel_id))
        .where(event_tags.c.event_id == event_id, event_tags.c.tag_kind == "channel")
    ).all()
    channel_tags = [{"slug": slug_v, "label": name, "kind": "channel"} for slug_v, name in channel_tag_rows]

    community_tag_rows = db.execute(
        select(communities.c.slug, communities.c.name)
        .select_from(event_tags.join(communities, communities.c.id == event_tags.c.community_id))
        .where(event_tags.c.event_id == event_id, event_tags.c.tag_kind == "community")
    ).all()
    community_tags = [{"slug": slug_v, "label": name, "kind": "community"} for slug_v, name in community_tag_rows]

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "event",
                content_votes.c.target_id == event_id,
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    attendees_rows = db.execute(
        select(event_attendance.c.user_id).where(
            event_attendance.c.event_id == event_id,
            event_attendance.c.attendance_state == "going",
        )
    ).all()
    attendee_ids = [user_id for (user_id,) in attendees_rows]
    attendees = [usernames.get(user_id, {}).get("username", "unknown") for user_id in attendee_ids]

    viewer_attendance_state = None
    if current_user_id is not None:
        viewer_attendance_state = db.execute(
            select(event_attendance.c.attendance_state).where(
                event_attendance.c.event_id == event_id,
                event_attendance.c.user_id == current_user_id,
            )
        ).scalar_one_or_none()

    members = [
        {
            "id": str(user_id),
            "username": usernames.get(user_id, {}).get("username", "unknown"),
            "bio": usernames.get(user_id, {}).get("bio", ""),
        }
        for user_id, _ in membership_rows
    ]
    event_editors_payload = [
        {
            "id": str(user_id),
            "username": usernames.get(user_id, {}).get("username", "unknown"),
            "bio": usernames.get(user_id, {}).get("bio", ""),
        }
        for user_id in editor_ids
    ]

    available_editor_invitees = [
        {
            "id": str(user_id),
            "username": usernames.get(user_id, {}).get("username", "unknown"),
            "bio": usernames.get(user_id, {}).get("bio", ""),
        }
        for user_id, _ in membership_rows
        if user_id not in editor_ids
    ]

    share_contact_rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio)
        .select_from(user_follows.join(users, users.c.id == user_follows.c.followed_id))
        .where(
            user_follows.c.follower_id == row["created_by"],
            user_follows.c.status == "accepted",
        )
        .limit(12)
    ).all()
    share_contacts = [
        {"id": str(user_id), "username": username, "bio": bio or ""}
        for user_id, username, bio in share_contact_rows
    ]

    viewer_signal = None
    if current_user_id is not None:
        viewer_signal = db.execute(
            select(event_signals.c.signal_type).where(
                event_signals.c.event_id == event_id,
                event_signals.c.user_id == current_user_id,
            )
        ).scalar_one_or_none()

    required_demand = required_votes(member_count)
    total_signals = signal_counts["total"]
    signal_ratio_percent = (signal_counts["demand"] / total_signals * 100.0) if total_signals > 0 else 0.0
    signal_summary = {
        "demandCount": signal_counts["demand"],
        "oppositionCount": signal_counts["opposition"],
        "totalCount": signal_counts["total"],
        "viewerSignal": viewer_signal,
        "signalRatioPercent": signal_ratio_percent,
        "ratioRequirementMet": signal_ratio_percent >= 66.0 if total_signals > 0 else False,
        "requiredDemandCount": required_demand,
        "demandRequirementMet": signal_counts["demand"] >= required_demand,
        "advancementUnlocked": signal_counts["demand"] >= required_demand,
        "usesPlatformVoteContext": False,
        "voteContextLabel": "Event members",
        "voteContextPopulation": member_count,
    }

    value_rows = db.execute(
        select(event_values.c.id, event_values.c.label, event_values.c.author_id)
        .where(event_values.c.event_id == event_id)
        .order_by(event_values.c.created_at.asc())
    ).all()
    value_ids = [value_id for value_id, _, _ in value_rows]
    importance_rows = db.execute(
        select(
            event_value_importance_votes.c.value_id,
            event_value_importance_votes.c.voter_id,
            event_value_importance_votes.c.importance,
        ).where(event_value_importance_votes.c.value_id.in_(value_ids or [UUID(int=0)]))
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

    plan_rows = db.execute(
        select(event_plans).where(event_plans.c.event_id == event_id).order_by(event_plans.c.created_at.desc())
    ).mappings().all()
    event_plans_payload = []
    winning_plan_id = None
    for plan in plan_rows:
        plan_vote_rows = db.execute(
            select(event_plan_votes.c.vote, event_plan_votes.c.voter_id).where(event_plan_votes.c.plan_id == plan["id"])
        ).all()
        overall_summary, _, _ = _vote_summary(plan_vote_rows, member_count, current_user_id)

        value_assessments = []
        for value_id, value_label, _ in value_rows:
            value_vote_rows = db.execute(
                select(event_plan_value_votes.c.vote, event_plan_value_votes.c.voter_id)
                .where(
                    event_plan_value_votes.c.plan_id == plan["id"],
                    event_plan_value_votes.c.value_id == value_id,
                )
            ).all()
            summary, _, _ = _vote_summary(value_vote_rows, member_count, current_user_id)
            value_assessments.append({
                "valueId": str(value_id),
                "valueLabel": value_label,
                **summary,
            })

        schedule_payload = dict(plan["schedule_payload"] or {})
        schedule_mode = str(schedule_payload.get("mode") or "any-day")
        start_date = schedule_payload.get("startDate") or schedule_payload.get("start_date")
        end_date = schedule_payload.get("endDate") or schedule_payload.get("end_date")
        start_time_label = schedule_payload.get("startTimeLabel") or schedule_payload.get("start_time_label")
        finish_time_label = schedule_payload.get("finishTimeLabel") or schedule_payload.get("finish_time_label")
        schedule = {
            "mode": schedule_mode,
            "startDate": start_date,
            "endDate": end_date,
            "startTimeLabel": start_time_label,
            "finishTimeLabel": finish_time_label,
            "label": schedule_payload.get("label") or schedule_mode.replace("-", " ").title(),
        }

        plan_payload = dict(plan["plan_payload"] or {})
        plan_phases = [
            {
                "id": str(item.get("id") or f"phase-{idx + 1}"),
                "title": str(item.get("title") or f"Phase {idx + 1}"),
                "details": str(item.get("details") or ""),
            }
            for idx, item in enumerate(list(plan_payload.get("planPhases") or []))
        ]

        event_plans_payload.append(
            {
                "id": str(plan["id"]),
                "title": plan["title"],
                "authorUsername": usernames.get(plan["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(plan["created_at"]),
                "description": plan["description"],
                "demandSignalSnapshot": signal_counts["demand"],
                "demandConsiderationNote": plan["demand_consideration_note"] or "",
                "locationLabel": plan["location_label"],
                "schedule": schedule,
                "planPhases": plan_phases,
                "valueAssessments": value_assessments,
                "overallApproval": overall_summary,
                "isLeading": bool(plan["is_leading"]),
            }
        )
        if plan["is_leading"]:
            winning_plan_id = str(plan["id"])

    activities_rows = db.execute(
        select(event_activities).where(event_activities.c.event_id == event_id).order_by(event_activities.c.scheduled_at.desc())
    ).mappings().all()
    activity_ids = [row_v["id"] for row_v in activities_rows]
    role_rows = db.execute(
        select(event_activity_roles).where(event_activity_roles.c.activity_id.in_(activity_ids or [UUID(int=0)]))
    ).mappings().all() if activity_ids else []

    role_ids = [role["id"] for role in role_rows]
    assignment_rows = db.execute(
        select(event_activity_assignments.c.role_id, event_activity_assignments.c.user_id)
        .where(event_activity_assignments.c.role_id.in_(role_ids or [UUID(int=0)]))
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
                "statusTone": "green",
                "roles": activity_roles,
                "note": activity["note"],
                "isActive": True,
            }
        )

    selectable_plan_phases = []
    for plan in event_plans_payload:
        for phase in plan["planPhases"]:
            selectable_plan_phases.append({"id": phase["id"], "label": phase["title"]})

    updates_rows = db.execute(
        select(event_updates).where(event_updates.c.event_id == event_id).order_by(event_updates.c.created_at.desc())
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

    update_request_rows = db.execute(
        select(event_update_requests).where(event_update_requests.c.event_id == event_id).order_by(event_update_requests.c.created_at.desc())
    ).mappings().all()
    update_requests = []
    for req in update_request_rows:
        vote_rows = db.execute(
            select(event_update_request_votes.c.vote, event_update_request_votes.c.voter_id)
            .where(event_update_request_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, member_count, current_user_id)
        update_requests.append(
            {
                "id": str(req["id"]),
                "body": req["body"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    edit_request_rows = db.execute(
        select(event_edit_requests).where(event_edit_requests.c.event_id == event_id).order_by(event_edit_requests.c.created_at.desc())
    ).mappings().all()
    edit_requests = []
    for req in edit_request_rows:
        vote_rows = db.execute(
            select(event_edit_request_votes.c.vote, event_edit_request_votes.c.voter_id)
            .where(event_edit_request_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, member_count, current_user_id)
        edit_requests.append(
            {
                "id": str(req["id"]),
                "title": req["title"],
                "description": req["description"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    phase_change_rows = db.execute(
        select(event_phase_change_requests)
        .where(event_phase_change_requests.c.event_id == event_id)
        .order_by(event_phase_change_requests.c.created_at.desc())
    ).mappings().all()
    phase_title_map = {item[0]: item[3] for item in EVENT_PHASES}
    phase_change_requests = []
    for req in phase_change_rows:
        vote_rows = db.execute(
            select(event_phase_change_votes.c.vote, event_phase_change_votes.c.voter_id)
            .where(event_phase_change_votes.c.request_id == req["id"])
        ).all()
        summary, passes, can_still = _vote_summary(vote_rows, member_count, current_user_id)
        phase_change_requests.append(
            {
                "id": str(req["id"]),
                "targetPhaseId": req["target_phase_id"],
                "targetPhaseLabel": phase_title_map.get(req["target_phase_id"], req["target_phase_id"]),
                "reason": req["reason"],
                "authorUsername": usernames.get(req["author_id"], {}).get("username", "unknown"),
                "createdAt": _iso(req["created_at"]),
                "kind": req["change_kind"],
                "approvalThresholdPercent": 66,
                "voteSummary": summary,
                "passesApprovalThreshold": passes,
                "canStillPass": can_still,
            }
        )

    phase_order = {phase_id: order for phase_id, order, _, _, _ in EVENT_PHASES}
    current_order = phase_order.get(row["current_phase_id"], 1)
    previous_phase = next((p for p in EVENT_PHASES if p[1] == current_order - 1), None)
    next_phase = next((p for p in EVENT_PHASES if p[1] == current_order + 1), None)

    lifecycle = {
        "currentPhaseId": row["current_phase_id"],
        "quorumThresholdPercent": (required_votes(member_count) / member_count * 100.0) if member_count > 0 else 0.0,
        "quorumVotesRequired": required_votes(member_count),
        "voteContextLabel": "Event members",
        "voteContextPopulation": member_count,
        "phases": _event_lifecycle_phases(row["current_phase_id"]),
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
            "plans": event_plans_payload,
            "winningPlanId": winning_plan_id,
            "viewerCanSubmitPlans": viewer_is_member,
            "viewerCanVoteOnPlans": viewer_is_member,
        },
        "activity": {
            "activities": activities,
            "viewerCanCreateActivities": viewer_is_member,
            "selectablePlanPhases": selectable_plan_phases,
        },
        "viewerCanRequestPhaseChanges": viewer_is_member,
        "viewerCanVoteOnPhaseChanges": viewer_is_member,
        "phaseChangeRequests": phase_change_requests,
        "revertablePhaseIds": [phase_id for phase_id, order, _, _, _ in EVENT_PHASES if order < current_order],
        "previousPhaseId": previous_phase[0] if previous_phase else None,
        "previousPhaseLabel": previous_phase[3] if previous_phase else None,
        "nextPhaseId": next_phase[0] if next_phase else None,
        "nextPhaseLabel": next_phase[3] if next_phase else None,
    }

    report_row = db.execute(
        select(reports.c.id, reports.c.reason, reports.c.description, reports.c.created_at, reports.c.resolution)
        .where(reports.c.target_type == "event", reports.c.target_id == event_id)
        .limit(1)
    ).mappings().first()
    report = None
    is_removed = False
    if report_row is not None:
        report = {
            "id": str(report_row["id"]),
            "subjectId": str(event_id),
            "targetId": str(event_id),
            "reason": report_row["reason"],
            "description": report_row["description"],
            "createdAt": _iso(report_row["created_at"]),
            "authorUsername": creator_username,
            "resolution": report_row["resolution"],
            "voteSummary": {
                "yesCount": 0,
                "noCount": 0,
                "activeVote": None,
                "eligibleVoterCount": member_count,
                "votesRequired": required_votes(member_count),
            },
        }
        is_removed = report_row["resolution"] in {"hidden", "removed"}

    discussion_rows = db.execute(
        select(comments.c.id, comments.c.author_id, comments.c.body, comments.c.created_at, comments.c.vote_count)
        .where(comments.c.subject_type == "event", comments.c.subject_id == event_id, comments.c.parent_id.is_(None))
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

    viewer_has_edit_access = bool(current_user_id is not None and (current_user_id == row["created_by"] or current_user_id in editor_ids))

    return {
        "id": str(event_id),
        "slug": row["slug"],
        "createdAt": _iso(row["created_at"]),
        "title": row["title"],
        "description": row["description"],
        "isPrivate": bool(row["is_private"]),
        "scheduledAt": _iso(row["scheduled_at"]),
        "channelTags": channel_tags,
        "communityTags": community_tags,
        "createdByUsername": creator_username,
        "timeLabel": row["time_label"],
        "locationLabel": row["location_label"],
        "voteCount": int(row["vote_count"] or 0),
        "activeVote": active_vote,
        "commentCount": int(row["comment_count"] or 0),
        "goingCount": int(row["going_count"] or 0),
        "memberCount": member_count,
        "lastActivityAt": _iso(row["last_activity_at"]),
        "signalSummary": signal_summary,
        "lifecycle": lifecycle,
        "attendanceNote": "",
        "agenda": [],
        "updates": updates,
        "updateRequests": update_requests,
        "viewerCanRequestUpdate": viewer_is_member,
        "viewerCanVoteOnUpdateRequests": viewer_is_member,
        "editRequests": edit_requests,
        "viewerCanRequestEdit": viewer_is_member,
        "viewerCanVoteOnEditRequests": viewer_is_member,
        "history": [],
        "attendees": attendees,
        "invitedUsernames": [],
        "eventEditors": event_editors_payload,
        "members": members,
        "viewerIsGoing": viewer_attendance_state == "going",
        "viewerCanToggleGoing": current_user_id is not None,
        "viewerHasEventEditAccess": viewer_has_edit_access,
        "viewerCanManageEditors": current_user_id is not None and current_user_id == row["created_by"],
        "viewerCanShare": viewer_is_member,
        "availableEditorInvitees": available_editor_invitees,
        "shareContacts": share_contacts,
        "report": report,
        "isRemovedByReport": is_removed,
        "discussionNote": "",
        "discussion": discussion,
    }


def join_event(db: Session, current_user_id: UUID, slug: str) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)

    inserted = False
    try:
        db.execute(
            insert(event_memberships).values(
                event_id=event_row["id"],
                user_id=current_user_id,
                role="member",
                joined_at=datetime.now(timezone.utc),
            )
        )
        inserted = True
    except IntegrityError:
        db.rollback()

    if inserted:
        db.execute(
            update(events)
            .where(events.c.id == event_row["id"])
            .values(member_count=events.c.member_count + 1)
        )
        db.commit()

    return {"ok": True, "joined": True, "slug": event_row["slug"]}


def toggle_event_attendance(
    db: Session,
    current_user_id: UUID,
    slug: str,
    attendance_state: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    normalized_state = attendance_state.strip().lower()

    if normalized_state not in EVENT_ATTENDANCE_STATES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"attendance_state must be one of: {sorted(EVENT_ATTENDANCE_STATES)}",
        )

    existing = db.execute(
        select(event_attendance.c.event_id, event_attendance.c.user_id, event_attendance.c.attendance_state)
        .where(
            event_attendance.c.event_id == event_row["id"],
            event_attendance.c.user_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    action = "none"
    going_count_delta = 0

    try:
        if existing is None:
            db.execute(
                insert(event_attendance).values(
                    event_id=event_row["id"],
                    user_id=current_user_id,
                    attendance_state=normalized_state,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            action = "added"
            if normalized_state == "going":
                going_count_delta = 1
        elif existing["attendance_state"] == normalized_state:
            db.execute(
                delete(event_attendance).where(
                    event_attendance.c.event_id == event_row["id"],
                    event_attendance.c.user_id == current_user_id,
                )
            )
            action = "removed"
            if normalized_state == "going":
                going_count_delta = -1
        else:
            db.execute(
                update(event_attendance)
                .where(
                    event_attendance.c.event_id == event_row["id"],
                    event_attendance.c.user_id == current_user_id,
                )
                .values(attendance_state=normalized_state, updated_at=datetime.now(timezone.utc))
            )
            action = "switched"
            if existing["attendance_state"] == "going" and normalized_state == "not-going":
                going_count_delta = -1
            elif existing["attendance_state"] == "not-going" and normalized_state == "going":
                going_count_delta = 1

        if going_count_delta != 0:
            db.execute(
                update(events)
                .where(events.c.id == event_row["id"])
                .values(going_count=func.greatest(events.c.going_count + going_count_delta, 0))
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not toggle attendance",
        ) from exc

    current = db.execute(
        select(event_attendance.c.attendance_state)
        .where(
            event_attendance.c.event_id == event_row["id"],
            event_attendance.c.user_id == current_user_id,
        )
        .limit(1)
    ).scalar_one_or_none()

    refreshed_going = db.execute(
        select(events.c.going_count).where(events.c.id == event_row["id"])
    ).scalar_one()

    return {
        "ok": True,
        "slug": event_row["slug"],
        "action": action,
        "attendance_state": current,
        "going_count": int(refreshed_going),
    }


async def toggle_event_signal(
    db: Session,
    cache: Redis,
    current_user_id: UUID,
    slug: str,
    signal_type: str,
) -> dict[str, object]:
    event_row = _get_event_by_slug_row(db, slug)
    normalized_signal = signal_type.strip().lower()

    if normalized_signal not in EVENT_SIGNAL_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"signal_type must be one of: {sorted(EVENT_SIGNAL_TYPES)}",
        )

    existing = db.execute(
        select(event_signals.c.id, event_signals.c.signal_type)
        .where(
            event_signals.c.event_id == event_row["id"],
            event_signals.c.user_id == current_user_id,
        )
        .limit(1)
    ).mappings().first()

    action = "none"

    try:
        if existing is None:
            db.execute(
                insert(event_signals).values(
                    event_id=event_row["id"],
                    user_id=current_user_id,
                    signal_type=normalized_signal,
                )
            )
            action = "added"
        elif existing["signal_type"] == normalized_signal:
            db.execute(delete(event_signals).where(event_signals.c.id == existing["id"]))
            action = "removed"
        else:
            db.execute(
                update(event_signals)
                .where(event_signals.c.id == existing["id"])
                .values(signal_type=normalized_signal)
            )
            action = "switched"

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not toggle signal") from exc

    counts = _get_signal_counts_db(db, event_row["id"])
    await _write_signal_counts_cache(cache, event_row["id"], counts)

    return {
        "ok": True,
        "slug": event_row["slug"],
        "action": action,
        "signal_type": normalized_signal,
        "signals": counts,
    }