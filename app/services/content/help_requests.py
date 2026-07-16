from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    content_votes,
    help_request_role_assignments,
    help_request_roles,
    help_request_tags,
    help_requests,
    users,
)
from app.services.access_control import (
    assert_can_view_entity,
)
from app.services.content.roles import (
    _load_help_request_roles,
)
from app.services.content.scopes import (
    _get_help_request_tags_enriched,
    _resolve_channel_ids,
    _resolve_community_ids,
)
from app.services.content.threads import _attach_usernames_to_comments
from app.services.governance import get_comments
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification

VALID_AUDIENCE = frozenset({"public", "followers"})


def _serialize_help_request(
    row: Mapping[str, object],
    roles: list[dict[str, object]] | None = None,
    active_vote: int = 0,
    discussion: list[dict[str, object]] | None = None,
    channel_tags: list[dict[str, object]] | None = None,
    community_tags: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": row["id"],
        "author_id": row["author_id"],
        "author_username": row.get("author_username", "") or "",
        "title": row["title"],
        "body": row["body"],
        "location_label": row["location_label"],
        "schedule_label": row["schedule_label"],
        "needed_at": row["needed_at"],
        "roles": roles if roles is not None else row.get("roles", []),
        "vote_count": int(row.get("vote_count") or 0),
        "comment_count": int(row.get("comment_count") or 0),
        "active_vote": active_vote,
        "discussion": discussion or [],
        "channel_tags": channel_tags or [],
        "community_tags": community_tags or [],
        "created_at": row["created_at"],
    }


def _format_needed_at_label(needed_at: datetime) -> str:
    return needed_at.strftime("%a %b %d, %Y at %H:%M")


def activity_status_tone(committed_count: int, minimum_participants: int) -> str:
    if committed_count <= 0:
        return "red"
    if minimum_participants > 0 and committed_count < minimum_participants:
        return "yellow"
    return "green"


def format_schedule_rail_label(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    label = value.strftime("%a %b %d, %Y at %H:%M")
    tz_name = value.strftime("%Z") or "UTC"
    return f"{label} {tz_name}"


def _validate_help_request_roles(roles: list[object]) -> list[dict[str, object]]:
    if not roles:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one role is required",
        )
    validated: list[dict[str, object]] = []
    for role in roles:
        if not isinstance(role, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each role must be an object with title, description, and slots",
            )
        title = str(role.get("title", "")).strip()
        description = str(role.get("description", "")).strip()
        slots = role.get("slots")
        if not title:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each role requires a title",
            )
        if slots is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each role requires slots",
            )
        try:
            slots_int = int(slots)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each role slots must be an integer",
            ) from exc
        if slots_int < 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Each role slots must be zero or greater",
            )
        validated.append({"title": title, "description": description, "slots": slots_int})
    return validated


def create_help_request(
    db: Session,
    current_user_id: UUID,
    title: str,
    body: str,
    location_label: str,
    needed_at: datetime,
    roles: list[object],
    channel_slugs: list[str],
    community_slugs: list[str] | None = None,
) -> dict[str, object]:
    community_slugs = community_slugs or []
    if not channel_slugs and not community_slugs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Help requests require at least one channel or community tag",
        )

    if needed_at.tzinfo is None:
        needed_at = needed_at.replace(tzinfo=UTC)

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    community_ids = _resolve_community_ids(db, community_slugs, current_user_id)
    validated_roles = _validate_help_request_roles(roles)
    schedule_label = _format_needed_at_label(needed_at)
    try:
        created = (
            db.execute(
                insert(help_requests)
                .values(
                    author_id=current_user_id,
                    title=title.strip(),
                    body=body.strip(),
                    location_label=location_label.strip(),
                    schedule_label=schedule_label,
                    needed_at=needed_at,
                    roles=validated_roles,
                )
                .returning(
                    help_requests.c.id,
                    help_requests.c.author_id,
                    help_requests.c.title,
                    help_requests.c.body,
                    help_requests.c.location_label,
                    help_requests.c.schedule_label,
                    help_requests.c.needed_at,
                    help_requests.c.roles,
                    help_requests.c.created_at,
                )
            )
            .mappings()
            .one()
        )

        inserted_roles = []
        for index, role in enumerate(validated_roles):
            role_row = (
                db.execute(
                    insert(help_request_roles)
                    .values(
                        help_request_id=created["id"],
                        title=role["title"],
                        description=role["description"],
                        slots=role["slots"],
                        sort_order=index,
                    )
                    .returning(
                        help_request_roles.c.id,
                        help_request_roles.c.title,
                        help_request_roles.c.description,
                        help_request_roles.c.slots,
                    )
                )
                .mappings()
                .one()
            )
            inserted_roles.append(role_row)

        for channel_id in channel_ids:
            db.execute(
                insert(help_request_tags).values(
                    help_request_id=created["id"],
                    tag_kind="channel",
                    channel_id=channel_id,
                    community_id=None,
                )
            )

        for community_id in community_ids:
            db.execute(
                insert(help_request_tags).values(
                    help_request_id=created["id"],
                    tag_kind="community",
                    channel_id=None,
                    community_id=community_id,
                )
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-help-request",
            metadata={"help_request_id": str(created["id"])},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create help request",
        ) from exc

    author_row = db.execute(
        select(users.c.username).where(users.c.id == current_user_id).limit(1)
    ).first()
    created_with_username = dict(created)
    created_with_username["author_username"] = author_row[0] if author_row else ""
    serialized_roles = [
        {
            "role_id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "slots": int(row["slots"]),
            "filled_count": 0,
            "is_viewer_assigned": False,
        }
        for row in inserted_roles
    ]
    return {"help_request": _serialize_help_request(created_with_username, serialized_roles)}


def get_help_request_by_id(
    db: Session,
    help_request_id: UUID,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    row = (
        db.execute(
            select(
                help_requests.c.id,
                help_requests.c.author_id,
                help_requests.c.title,
                help_requests.c.body,
                help_requests.c.location_label,
                help_requests.c.schedule_label,
                help_requests.c.needed_at,
                help_requests.c.vote_count,
                help_requests.c.comment_count,
                help_requests.c.created_at,
                users.c.username.label("author_username"),
            )
            .select_from(help_requests.outerjoin(users, users.c.id == help_requests.c.author_id))
            .where(help_requests.c.id == help_request_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Help request not found")

    assert_can_view_entity(db, current_user_id, "help_request", row["id"])

    roles = _load_help_request_roles(db, [help_request_id], current_user_id).get(
        str(help_request_id), []
    )

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "help_request",
                content_votes.c.target_id == row["id"],
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    comments_result = get_comments(
        db,
        subject_type="help_request",
        subject_id=row["id"],
        current_user_id=current_user_id,
    )
    discussion = _attach_usernames_to_comments(db, comments_result["items"])
    channel_tags, community_tags = _get_help_request_tags_enriched(db, help_request_id)

    return {
        "help_request": _serialize_help_request(
            row,
            roles,
            active_vote=active_vote,
            discussion=discussion,
            channel_tags=channel_tags,
            community_tags=community_tags,
        )
    }


def commit_help_request_role(
    db: Session,
    current_user_id: UUID,
    help_request_id: UUID,
    role_id: UUID,
) -> dict[str, object]:
    help_request_row = (
        db.execute(
            select(
                help_requests.c.id,
                help_requests.c.author_id,
                help_requests.c.title,
            ).where(help_requests.c.id == help_request_id)
        )
        .mappings()
        .first()
    )
    if help_request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Help request not found")

    role_row = (
        db.execute(
            select(
                help_request_roles.c.id,
                help_request_roles.c.help_request_id,
                help_request_roles.c.title,
                help_request_roles.c.slots,
            ).where(
                help_request_roles.c.id == role_id,
                help_request_roles.c.help_request_id == help_request_id,
            )
        )
        .mappings()
        .first()
    )
    if role_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    existing_assignment = db.execute(
        select(help_request_role_assignments.c.role_id)
        .select_from(
            help_request_role_assignments.join(
                help_request_roles,
                help_request_roles.c.id == help_request_role_assignments.c.role_id,
            )
        )
        .where(
            help_request_roles.c.help_request_id == help_request_id,
            help_request_role_assignments.c.user_id == current_user_id,
        )
    ).first()
    if existing_assignment is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already assigned in this help request",
        )

    filled_count = db.execute(
        select(func.count())
        .select_from(help_request_role_assignments)
        .where(help_request_role_assignments.c.role_id == role_id)
    ).scalar_one()
    slots = int(role_row["slots"])
    if slots > 0 and int(filled_count) >= slots:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Role is already full")

    try:
        db.execute(
            insert(help_request_role_assignments).values(role_id=role_id, user_id=current_user_id)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not commit help request role",
        ) from exc

    author_id = help_request_row["author_id"]
    if author_id is not None and author_id != current_user_id:
        create_notification(
            db=db,
            recipient_id=author_id,
            actor_id=current_user_id,
            kind="hr-role-signup",
            surface="public",
            subject_type="help-request",
            subject_id=help_request_id,
            target_id=role_id,
            title=str(help_request_row["title"]),
            body=f'Someone signed up for the "{role_row["title"]}" role.',
            href=f"/help-requests/{help_request_id}",
        )

    return {
        "ok": True,
        "help_request_id": help_request_id,
        "role_id": role_id,
        "user_id": current_user_id,
    }


def uncommit_help_request_role(
    db: Session,
    current_user_id: UUID,
    help_request_id: UUID,
    role_id: UUID,
) -> dict[str, object]:
    help_request_row = db.execute(
        select(help_requests.c.id).where(help_requests.c.id == help_request_id)
    ).first()
    if help_request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Help request not found")

    role_row = db.execute(
        select(help_request_roles.c.id).where(
            help_request_roles.c.id == role_id,
            help_request_roles.c.help_request_id == help_request_id,
        )
    ).first()
    if role_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")

    db.execute(
        delete(help_request_role_assignments).where(
            help_request_role_assignments.c.role_id == role_id,
            help_request_role_assignments.c.user_id == current_user_id,
        )
    )
    db.commit()
    return {"ok": True, "help_request_id": help_request_id, "role_id": role_id}
