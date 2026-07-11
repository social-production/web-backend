from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    event_activity_assignments,
    event_activity_ratings,
    event_activity_roles,
    event_activities,
    event_memberships,
    project_activity_assignments,
    project_activity_ratings,
    project_activity_roles,
    project_activities,
    project_memberships,
    project_service_history_completions,
    project_service_requests,
    projects,
    events,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def ensure_future_scheduled_start(scheduled_at: datetime) -> None:
    if ensure_aware(scheduled_at) < utc_now():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="scheduled_at must be in the future",
        )


def ensure_activity_roles_unlocked(ends_at: datetime) -> None:
    if ensure_aware(ends_at) <= utc_now():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Activity has ended; roles are locked",
        )


def is_activity_ended(ends_at: datetime, now: datetime | None = None) -> bool:
    reference = now or utc_now()
    return ensure_aware(ends_at) <= reference


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return ensure_aware(value).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _rating_summary(ratings: list[dict[str, object]]) -> dict[str, object]:
    if not ratings:
        return {"average": None, "count": 0}
    total = sum(int(item["rating"]) for item in ratings)
    return {"average": round(total / len(ratings), 1), "count": len(ratings)}


def _staffing_failed(committed_count: int, minimum_participants: int) -> bool:
    return minimum_participants > 0 and committed_count < minimum_participants


def _ensure_participant_auto_uncompleted(db: Session, activity_id: UUID) -> None:
    db.execute(
        update(project_activities)
        .where(
            project_activities.c.id == activity_id,
            project_activities.c.participant_auto_uncompleted_at.is_(None),
        )
        .values(participant_auto_uncompleted_at=utc_now())
    )


def _auto_uncompleted_participant_state(*, label: str = "Participants") -> dict[str, object]:
    return {
        "label": label,
        "totalEligible": 0,
        "completedCount": 0,
        "uncompletedCount": 1,
        "pendingCount": 0,
        "viewerCanSet": False,
        "viewerSelection": None,
        "doneCount": 0,
        "viewerCanToggle": False,
        "viewerHasMarkedDone": False,
        "systemAutoUncompleted": True,
        "statusLabel": "Marked uncompleted — minimum staffing not met",
    }


def _completion_side_state(
    *,
    label: str,
    eligible_user_ids: list[UUID],
    completion_rows: list[Mapping[str, object]],
    current_user_id: UUID | None,
) -> dict[str, object]:
    completed_ids = {
        row["requester_user_id"] or row["participant_user_id"]
        for row in completion_rows
        if row["completion_state"] == "completed"
    }
    uncompleted_ids = {
        row["requester_user_id"] or row["participant_user_id"]
        for row in completion_rows
        if row["completion_state"] == "uncompleted"
    }
    total_eligible = len(eligible_user_ids)
    completed_count = len([user_id for user_id in eligible_user_ids if user_id in completed_ids])
    uncompleted_count = len([user_id for user_id in eligible_user_ids if user_id in uncompleted_ids])
    pending_count = max(0, total_eligible - completed_count - uncompleted_count)
    viewer_selection = None
    viewer_can_set = False
    if current_user_id is not None and current_user_id in eligible_user_ids:
        viewer_can_set = True
        viewer_row = next(
            (
                row
                for row in completion_rows
                if (row["requester_user_id"] or row["participant_user_id"]) == current_user_id
            ),
            None,
        )
        if viewer_row is not None:
            viewer_selection = viewer_row["completion_state"]

    return {
        "label": label,
        "totalEligible": total_eligible,
        "completedCount": completed_count,
        "uncompletedCount": uncompleted_count,
        "pendingCount": pending_count,
        "viewerCanSet": viewer_can_set,
        "viewerSelection": viewer_selection,
        "doneCount": completed_count,
        "viewerCanToggle": viewer_can_set,
        "viewerHasMarkedDone": viewer_selection == "completed",
    }


def _aggregate_completion(
    requester_state: dict[str, object] | None,
    participant_state: dict[str, object],
) -> tuple[str, str, str]:
    states: list[str] = []
    if requester_state is not None and int(requester_state["totalEligible"]) > 0:
        if int(requester_state["completedCount"]) > 0:
            states.append("completed")
        if int(requester_state["uncompletedCount"]) > 0:
            states.append("uncompleted")
        if int(requester_state["pendingCount"]) > 0:
            states.append("pending")
    if bool(participant_state.get("systemAutoUncompleted")):
        states.append("uncompleted")
    elif int(participant_state["totalEligible"]) > 0:
        if int(participant_state["completedCount"]) > 0:
            states.append("completed")
        if int(participant_state["uncompletedCount"]) > 0:
            states.append("uncompleted")
        if int(participant_state["pendingCount"]) > 0:
            states.append("pending")

    if not states or all(state == "pending" for state in states):
        return "mixed", "Completion pending", "mixed"
    if "uncompleted" in states and "completed" not in states:
        return "uncompleted", "Marked uncompleted", "uncompleted"
    if "completed" in states and "uncompleted" not in states and "pending" not in states:
        return "completed", "Marked completed", "complete"
    return "mixed", "Mixed completion", "mixed"


def _history_labels(
    *,
    linked_request_id: UUID | None,
    committed_count: int,
    minimum_participants: int,
) -> tuple[str, str, str]:
    if linked_request_id is None:
        return (
            "self-planned",
            "Self-planned activity",
            "This activity was scheduled directly on the project calendar.",
        )
    if committed_count >= minimum_participants and minimum_participants > 0:
        return (
            "committed-activity",
            "Committed activity",
            "Enough participants were assigned before this activity ended.",
        )
    return (
        "planned-activity",
        "Planned activity",
        "This request-linked activity ended before minimum staffing was met.",
    )


def build_project_history_items(
    db: Session,
    *,
    project_id: UUID,
    ended_activities: list[dict[str, object]],
    activity_rows_by_id: dict[UUID, Mapping[str, object]],
    assignments_by_activity: dict[UUID, set[UUID]],
    usernames: dict[UUID, dict[str, object]],
    current_user_id: UUID | None,
    ratings_by_activity: dict[UUID, list[dict[str, object]]],
) -> list[dict[str, object]]:
    if not ended_activities:
        return []

    activity_ids = [UUID(activity["id"]) for activity in ended_activities]
    completion_rows = db.execute(
        select(project_service_history_completions).where(
            project_service_history_completions.c.project_id == project_id,
            project_service_history_completions.c.history_item_key.in_([str(activity_id) for activity_id in activity_ids]),
        )
    ).mappings().all()
    completions_by_key: dict[str, list[Mapping[str, object]]] = {}
    for row in completion_rows:
        completions_by_key.setdefault(row["history_item_key"], []).append(row)

    request_rows = db.execute(
        select(project_service_requests).where(
            project_service_requests.c.project_id == project_id,
            project_service_requests.c.linked_activity_id.in_(activity_ids),
        )
    ).mappings().all()
    requests_by_activity = {row["linked_activity_id"]: row for row in request_rows if row["linked_activity_id"] is not None}

    history_items: list[dict[str, object]] = []
    for activity in ended_activities:
        activity_id = UUID(activity["id"])
        activity_row = activity_rows_by_id[activity_id]
        assigned_user_ids = sorted(assignments_by_activity.get(activity_id, set()), key=str)
        request_row = requests_by_activity.get(activity_id)
        linked_request_id = activity_row.get("linked_request_id")
        requester_user_id = request_row["requester_id"] if request_row is not None else None
        requester_username = (
            usernames.get(requester_user_id, {}).get("username", "unknown") if requester_user_id is not None else None
        )

        completion_rows_for_activity = completions_by_key.get(str(activity_id), [])
        requester_completion = None
        if requester_user_id is not None:
            requester_completion = _completion_side_state(
                label="Requester",
                eligible_user_ids=[requester_user_id],
                completion_rows=[
                    row for row in completion_rows_for_activity if row["role"] == "requester"
                ],
                current_user_id=current_user_id,
            )

        participant_completion = None
        staffing_failed = _staffing_failed(
            int(activity["committedCount"]),
            int(activity["minimumParticipants"]),
        )
        if staffing_failed:
            _ensure_participant_auto_uncompleted(db, activity_id)
            participant_completion = _auto_uncompleted_participant_state()
        else:
            participant_completion = _completion_side_state(
                label="Participants",
                eligible_user_ids=assigned_user_ids,
                completion_rows=[row for row in completion_rows_for_activity if row["role"] == "participants"],
                current_user_id=current_user_id,
            )

        aggregate_state, aggregate_label, aggregate_tone = _aggregate_completion(
            requester_completion,
            participant_completion,
        )
        history_state, history_state_label, history_state_description = _history_labels(
            linked_request_id=linked_request_id,
            committed_count=int(activity["committedCount"]),
            minimum_participants=int(activity["minimumParticipants"]),
        )

        ratings = ratings_by_activity.get(activity_id, [])
        viewer_rating = None
        viewer_can_rate = False
        if current_user_id is not None and current_user_id in assigned_user_ids:
            viewer_can_rate = True
            matched_rating = next(
                (item for item in ratings if item["userId"] == str(current_user_id)),
                None,
            )
            if matched_rating is not None:
                viewer_rating = {
                    "rating": matched_rating["rating"],
                    "comment": matched_rating["comment"],
                }

        history_items.append(
            {
                "id": str(activity_id),
                "source": "request" if linked_request_id is not None else "self-planned",
                "requestId": str(request_row["id"]) if request_row is not None else None,
                "requesterUsername": requester_username,
                "activity": {**activity, "isActive": False, "rolesLocked": True},
                "historyState": history_state,
                "historyStateLabel": history_state_label,
                "historyStateDescription": history_state_description,
                "aggregateCompletionState": aggregate_state,
                "aggregateCompletionLabel": aggregate_label,
                "aggregateCompletionTone": aggregate_tone,
                "requesterCompletion": requester_completion,
                "participantCompletion": participant_completion,
                "aggregateRating": _rating_summary(ratings),
                "ratings": ratings,
                "viewerCanRate": viewer_can_rate,
                "viewerRating": viewer_rating,
            }
        )

    return history_items


def build_event_history_items(
    *,
    ended_activities: list[dict[str, object]],
    assignments_by_activity: dict[UUID, set[UUID]],
    current_user_id: UUID | None,
    ratings_by_activity: dict[UUID, list[dict[str, object]]],
) -> list[dict[str, object]]:
    history_items: list[dict[str, object]] = []
    for activity in ended_activities:
        activity_id = UUID(activity["id"])
        assigned_user_ids = assignments_by_activity.get(activity_id, set())
        ratings = ratings_by_activity.get(activity_id, [])
        viewer_rating = None
        viewer_can_rate = False
        if current_user_id is not None and current_user_id in assigned_user_ids:
            viewer_can_rate = True
            matched_rating = next(
                (item for item in ratings if item["userId"] == str(current_user_id)),
                None,
            )
            if matched_rating is not None:
                viewer_rating = {
                    "rating": matched_rating["rating"],
                    "comment": matched_rating["comment"],
                }

        history_state, history_state_label, history_state_description = _history_labels(
            linked_request_id=None,
            committed_count=int(activity["committedCount"]),
            minimum_participants=int(activity["minimumParticipants"]),
        )

        participant_completion = _completion_side_state(
            label="Participants",
            eligible_user_ids=sorted(assigned_user_ids, key=str),
            completion_rows=[],
            current_user_id=current_user_id,
        )
        aggregate_state, aggregate_label, aggregate_tone = _aggregate_completion(None, participant_completion)

        history_items.append(
            {
                "id": str(activity_id),
                "source": "self-planned",
                "requestId": None,
                "requesterUsername": None,
                "activity": {**activity, "isActive": False, "rolesLocked": True},
                "historyState": history_state,
                "historyStateLabel": history_state_label,
                "historyStateDescription": history_state_description,
                "aggregateCompletionState": aggregate_state,
                "aggregateCompletionLabel": aggregate_label,
                "aggregateCompletionTone": aggregate_tone,
                "requesterCompletion": None,
                "participantCompletion": participant_completion,
                "aggregateRating": _rating_summary(ratings),
                "ratings": ratings,
                "viewerCanRate": viewer_can_rate,
                "viewerRating": viewer_rating,
            }
        )

    return history_items


def load_project_ratings_by_activity(
    db: Session,
    activity_ids: list[UUID],
    usernames: dict[UUID, dict[str, object]],
) -> dict[UUID, list[dict[str, object]]]:
    if not activity_ids:
        return {}
    rows = db.execute(
        select(project_activity_ratings).where(project_activity_ratings.c.activity_id.in_(activity_ids))
    ).mappings().all()
    grouped: dict[UUID, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(row["activity_id"], []).append(
            {
                "userId": str(row["user_id"]),
                "username": usernames.get(row["user_id"], {}).get("username", "unknown"),
                "rating": int(row["rating"]),
                "comment": row["comment"],
                "updatedAt": _iso(row["updated_at"]),
            }
        )
    return grouped


def load_event_ratings_by_activity(
    db: Session,
    activity_ids: list[UUID],
    usernames: dict[UUID, dict[str, object]],
) -> dict[UUID, list[dict[str, object]]]:
    if not activity_ids:
        return {}
    rows = db.execute(
        select(event_activity_ratings).where(event_activity_ratings.c.activity_id.in_(activity_ids))
    ).mappings().all()
    grouped: dict[UUID, list[dict[str, object]]] = {}
    for row in rows:
        grouped.setdefault(row["activity_id"], []).append(
            {
                "userId": str(row["user_id"]),
                "username": usernames.get(row["user_id"], {}).get("username", "unknown"),
                "rating": int(row["rating"]),
                "comment": row["comment"],
                "updatedAt": _iso(row["updated_at"]),
            }
        )
    return grouped


def _viewer_is_assigned_on_project_activity(db: Session, activity_id: UUID, user_id: UUID) -> bool:
    return (
        db.execute(
            select(project_activity_assignments.c.user_id)
            .select_from(
                project_activity_assignments.join(
                    project_activity_roles,
                    project_activity_roles.c.id == project_activity_assignments.c.role_id,
                )
            )
            .where(
                project_activity_roles.c.activity_id == activity_id,
                project_activity_assignments.c.user_id == user_id,
            )
        ).first()
        is not None
    )


def _viewer_is_assigned_on_event_activity(db: Session, activity_id: UUID, user_id: UUID) -> bool:
    return (
        db.execute(
            select(event_activity_assignments.c.user_id)
            .select_from(
                event_activity_assignments.join(
                    event_activity_roles,
                    event_activity_roles.c.id == event_activity_assignments.c.role_id,
                )
            )
            .where(
                event_activity_roles.c.activity_id == activity_id,
                event_activity_assignments.c.user_id == user_id,
            )
        ).first()
        is not None
    )


def upsert_project_activity_rating(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    rating: int,
    comment: str | None,
) -> dict[str, object]:
    project_row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if project_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    activity_row = db.execute(
        select(project_activities).where(
            project_activities.c.id == activity_id,
            project_activities.c.project_id == project_row["id"],
        )
    ).mappings().first()
    if activity_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
    if not is_activity_ended(activity_row["ends_at"]):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Activity has not ended yet")
    if not _viewer_is_assigned_on_project_activity(db, activity_id, current_user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only assigned participants can rate activities")

    normalized_comment = comment.strip() if comment else None
    existing = db.execute(
        select(project_activity_ratings).where(
            project_activity_ratings.c.activity_id == activity_id,
            project_activity_ratings.c.user_id == current_user_id,
        )
    ).mappings().first()

    try:
        if existing is None:
            db.execute(
                insert(project_activity_ratings).values(
                    activity_id=activity_id,
                    user_id=current_user_id,
                    rating=rating,
                    comment=normalized_comment,
                )
            )
        else:
            db.execute(
                update(project_activity_ratings)
                .where(
                    project_activity_ratings.c.activity_id == activity_id,
                    project_activity_ratings.c.user_id == current_user_id,
                )
                .values(rating=rating, comment=normalized_comment, updated_at=utc_now())
            )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save rating") from exc

    return {"ok": True, "activityId": str(activity_id), "rating": rating, "comment": normalized_comment}


def upsert_event_activity_rating(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
    rating: int,
    comment: str | None,
) -> dict[str, object]:
    event_row = db.execute(select(events).where(events.c.slug == slug.lower())).mappings().first()
    if event_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    activity_row = db.execute(
        select(event_activities).where(
            event_activities.c.id == activity_id,
            event_activities.c.event_id == event_row["id"],
        )
    ).mappings().first()
    if activity_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Activity not found")
    if not is_activity_ended(activity_row["ends_at"]):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Activity has not ended yet")
    if not _viewer_is_assigned_on_event_activity(db, activity_id, current_user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only assigned participants can rate activities")

    normalized_comment = comment.strip() if comment else None
    existing = db.execute(
        select(event_activity_ratings).where(
            event_activity_ratings.c.activity_id == activity_id,
            event_activity_ratings.c.user_id == current_user_id,
        )
    ).mappings().first()

    try:
        if existing is None:
            db.execute(
                insert(event_activity_ratings).values(
                    activity_id=activity_id,
                    user_id=current_user_id,
                    rating=rating,
                    comment=normalized_comment,
                )
            )
        else:
            db.execute(
                update(event_activity_ratings)
                .where(
                    event_activity_ratings.c.activity_id == activity_id,
                    event_activity_ratings.c.user_id == current_user_id,
                )
                .values(rating=rating, comment=normalized_comment, updated_at=utc_now())
            )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save rating") from exc

    return {"ok": True, "activityId": str(activity_id), "rating": rating, "comment": normalized_comment}


def delete_project_activity_rating(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
) -> dict[str, object]:
    project_row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if project_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    db.execute(
        delete(project_activity_ratings).where(
            project_activity_ratings.c.activity_id == activity_id,
            project_activity_ratings.c.user_id == current_user_id,
        )
    )
    db.commit()
    return {"ok": True, "activityId": str(activity_id)}


def delete_event_activity_rating(
    db: Session,
    current_user_id: UUID,
    slug: str,
    activity_id: UUID,
) -> dict[str, object]:
    event_row = db.execute(select(events).where(events.c.slug == slug.lower())).mappings().first()
    if event_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    db.execute(
        delete(event_activity_ratings).where(
            event_activity_ratings.c.activity_id == activity_id,
            event_activity_ratings.c.user_id == current_user_id,
        )
    )
    db.commit()
    return {"ok": True, "activityId": str(activity_id)}
