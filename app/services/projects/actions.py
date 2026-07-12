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
import importlib
_mod = importlib.import_module('app.services.projects.helpers')
globals().update({k: v for k, v in vars(_mod).items() if not k.startswith('__') or k == '__all__'})

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

    # Invalidate weekly-active caches so quorum drops immediately
    try:
        from app.cache import get_sync_redis_client
        redis = get_sync_redis_client()
        redis.delete(f"governance:weekly_active:project:{project_row['id']}")
        redis.delete("governance:weekly_active")
    except Exception:
        pass

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
    is_online: bool = False,
    linked_plan_id: UUID | None = None,
    linked_plan_phase_id: str | None = None,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    if project_row["project_mode"] == "personal-service":
        _ensure_personal_service_author(project_row, current_user_id)
    else:
        _ensure_project_member(db, project_row["id"], current_user_id)

    if ends_at <= scheduled_at:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ends_at must be after scheduled_at")
    ensure_future_scheduled_start(scheduled_at)

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
                is_online=is_online,
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
                project_activities.c.is_online,
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
    ensure_activity_roles_unlocked(activity_row["ends_at"])

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

    activity_row = db.execute(
        select(project_activities).where(
            project_activities.c.id == activity_id,
            project_activities.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if activity_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
    ensure_activity_roles_unlocked(activity_row["ends_at"])

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
    if project_row["project_mode"] == "personal-service":
        _ensure_personal_service_author(project_row, current_user_id)
    else:
        _ensure_project_member(db, project_row["id"], current_user_id)

    normalized_title = title.strip() or "Update"
    normalized_body = body.strip()
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


def update_project_details(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    description: str,
) -> dict[str, object]:
    project_row = _get_project_by_slug_row(db, slug)
    if project_row["project_mode"] != "personal-service":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Direct detail updates are only available for personal-service projects",
        )
    _ensure_personal_service_author(project_row, current_user_id)

    normalized_title = title.strip()
    normalized_description = description.strip()
    if not normalized_title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title is required")
    if not normalized_description:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="description is required")

    try:
        db.execute(
            update(projects)
            .where(projects.c.id == project_row["id"])
            .values(title=normalized_title, description=normalized_description)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not update project details") from exc

    return {"ok": True, "slug": project_row["slug"], "title": normalized_title, "description": normalized_description}


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
        body=f"A project was shared with you: {project_row['title']}. Open /projects/{project_row['slug']}",
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

        if action in {"added", "switched"}:
            record_meaningful_action(
                db=db,
                user_id=current_user_id,
                action_type="signal-demand" if normalized_signal == "demand" else "signal-opposition",
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
