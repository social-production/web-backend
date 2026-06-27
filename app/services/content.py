from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    content_votes,
    help_request_role_assignments,
    help_request_roles,
    help_request_tags,
    help_requests,
    posts,
    scope_memberships,
    thread_tags,
    threads,
    users,
)
from app.services.governance import get_comments
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
from app.services.search import index_document

VALID_AUDIENCE = frozenset({"public", "followers"})


def _serialize_thread(row: Mapping[str, object], tags: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "body": row["body"],
        "author_id": row["author_id"],
        "vote_count": row["vote_count"],
        "comment_count": row["comment_count"],
        "last_activity_at": row["last_activity_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "tags": tags,
    }


def _serialize_post(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "author_id": row["author_id"],
        "body": row["body"],
        "audience": row["audience"],
        "vote_count": row["vote_count"],
        "comment_count": row["comment_count"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_thread_tags(db: Session, thread_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(
            thread_tags.c.id,
            thread_tags.c.tag_kind,
            thread_tags.c.channel_id,
            thread_tags.c.community_id,
        ).where(thread_tags.c.thread_id == thread_id)
    ).mappings().all()
    return [dict(row) for row in rows]


def _attach_usernames_to_comments(
    db: Session, items: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Recursively attach author_username to comment dicts."""
    all_ids: set[UUID] = set()

    def _collect(comments: list[dict[str, object]]) -> None:
        for c in comments:
            if c.get("author_id"):
                all_ids.add(c["author_id"])
            _collect(c.get("replies") or [])

    _collect(items)

    username_map: dict[UUID, str] = {}
    if all_ids:
        rows = db.execute(
            select(users.c.id, users.c.username).where(users.c.id.in_(list(all_ids)))
        ).all()
        username_map = {row[0]: row[1] for row in rows}

    def _attach(comments: list[dict[str, object]]) -> list[dict[str, object]]:
        result = []
        for c in comments:
            item = dict(c)
            item["author_username"] = username_map.get(item.get("author_id"), "")
            item["replies"] = _attach(item.get("replies") or [])
            result.append(item)
        return result

    return _attach(items)


def _get_thread_tags_enriched(db: Session, thread_id: UUID) -> tuple[list[dict], list[dict]]:
    """Returns (channel_tags, community_tags) each as [{slug, label, kind}]."""
    rows = db.execute(
        select(
            thread_tags.c.tag_kind,
            channels.c.slug.label("channel_slug"),
            channels.c.name.label("channel_name"),
            communities.c.slug.label("community_slug"),
            communities.c.name.label("community_name"),
        )
        .select_from(thread_tags)
        .outerjoin(channels, channels.c.id == thread_tags.c.channel_id)
        .outerjoin(communities, communities.c.id == thread_tags.c.community_id)
        .where(thread_tags.c.thread_id == thread_id)
    ).mappings().all()

    channel_tags = [
        {"slug": r["channel_slug"], "label": r["channel_name"], "kind": "channel"}
        for r in rows if r["channel_slug"]
    ]
    community_tags = [
        {"slug": r["community_slug"], "label": r["community_name"], "kind": "community"}
        for r in rows if r["community_slug"]
    ]
    return channel_tags, community_tags


def _get_help_request_tags_enriched(db: Session, help_request_id: UUID) -> tuple[list[dict], list[dict]]:
    """Returns (channel_tags, community_tags) each as [{slug, label, kind}]."""
    rows = db.execute(
        select(
            help_request_tags.c.tag_kind,
            channels.c.slug.label("channel_slug"),
            channels.c.name.label("channel_name"),
            communities.c.slug.label("community_slug"),
            communities.c.name.label("community_name"),
        )
        .select_from(help_request_tags)
        .outerjoin(channels, channels.c.id == help_request_tags.c.channel_id)
        .outerjoin(communities, communities.c.id == help_request_tags.c.community_id)
        .where(help_request_tags.c.help_request_id == help_request_id)
    ).mappings().all()

    channel_tags = [
        {"slug": r["channel_slug"], "label": r["channel_name"], "kind": "channel"}
        for r in rows if r["channel_slug"]
    ]
    community_tags = [
        {"slug": r["community_slug"], "label": r["community_name"], "kind": "community"}
        for r in rows if r["community_slug"]
    ]
    return channel_tags, community_tags


def _resolve_channel_ids(db: Session, channel_slugs: list[str]) -> list[UUID]:
    """Return the UUIDs for the given channel slugs, raising 404 for any unknown slug."""
    normalized = [s.strip().lower() for s in channel_slugs if s.strip()]
    if not normalized:
        return []
    rows = db.execute(
        select(channels.c.id, channels.c.slug).where(channels.c.slug.in_(normalized))
    ).mappings().all()
    found_slugs = {row["slug"] for row in rows}
    missing = set(normalized) - found_slugs
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown channel slugs: {sorted(missing)}",
        )
    return [row["id"] for row in rows]


def _resolve_community_ids(db: Session, community_slugs: list[str], current_user_id: UUID) -> list[UUID]:
    """Return the UUIDs for the given community slugs, raising 422 for any unknown slug."""
    normalized = [s.strip().lower() for s in community_slugs if s.strip()]
    if not normalized:
        return []
    rows = db.execute(
        select(communities.c.id, communities.c.slug, communities.c.join_policy).where(communities.c.slug.in_(normalized))
    ).mappings().all()
    found_slugs = {row["slug"] for row in rows}
    missing = set(normalized) - found_slugs
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown community slugs: {sorted(missing)}",
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


def create_thread(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    body: str,
    channel_slugs: list[str],
    community_slugs: list[str] | None = None,
) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")

    community_slugs = community_slugs or []
    if not channel_slugs and not community_slugs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Threads require at least one channel or community tag",
        )

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    community_ids = _resolve_community_ids(db, community_slugs, current_user_id)

    now = datetime.now(timezone.utc)

    try:
        thread_row = db.execute(
            insert(threads)
            .values(
                slug=normalized_slug,
                title=title.strip(),
                body=body.strip(),
                author_id=current_user_id,
                last_activity_at=now,
            )
            .returning(
                threads.c.id,
                threads.c.slug,
                threads.c.title,
                threads.c.body,
                threads.c.author_id,
                threads.c.vote_count,
                threads.c.comment_count,
                threads.c.last_activity_at,
                threads.c.created_at,
                threads.c.updated_at,
            )
        ).mappings().one()

        for channel_id in channel_ids:
            db.execute(
                insert(thread_tags).values(
                    thread_id=thread_row["id"],
                    tag_kind="channel",
                    channel_id=channel_id,
                    community_id=None,
                )
            )

        for community_id in community_ids:
            db.execute(
                insert(thread_tags).values(
                    thread_id=thread_row["id"],
                    tag_kind="community",
                    channel_id=None,
                    community_id=community_id,
                )
            )

        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-thread",
            metadata={"thread_id": str(thread_row["id"]), "slug": thread_row["slug"]},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Thread slug already exists") from exc

    tags = _get_thread_tags(db, thread_row["id"])
    index_document(
        db=db,
        entity_type="thread",
        entity_id=thread_row["id"],
        title=thread_row["title"],
        summary=thread_row["body"],
        meta="thread",
        href=f"/threads/{thread_row['slug']}",
    )
    return {"thread": _serialize_thread(thread_row, tags)}


def get_thread_by_slug(db: Session, slug: str, current_user_id: UUID | None = None) -> dict[str, object]:
    row = db.execute(
        select(
            threads.c.id, threads.c.slug, threads.c.title, threads.c.body,
            threads.c.author_id, threads.c.vote_count, threads.c.comment_count,
            threads.c.last_activity_at, threads.c.created_at, threads.c.updated_at,
            users.c.username.label("author_username"),
        )
        .select_from(threads.outerjoin(users, users.c.id == threads.c.author_id))
        .where(threads.c.slug == slug.lower())
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "thread",
                content_votes.c.target_id == row["id"],
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    channel_tags, community_tags = _get_thread_tags_enriched(db, row["id"])
    comments_result = get_comments(db, subject_type="thread", subject_id=row["id"], current_user_id=current_user_id)
    discussion = _attach_usernames_to_comments(db, comments_result["items"])

    return {
        "thread": {
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "body": row["body"],
            "author_id": row["author_id"],
            "author_username": row["author_username"] or "",
            "vote_count": row["vote_count"],
            "comment_count": row["comment_count"],
            "last_activity_at": row["last_activity_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "active_vote": active_vote,
            "channel_tags": channel_tags,
            "community_tags": community_tags,
            "discussion": discussion,
        }
    }


def create_post(
    db: Session,
    current_user_id: UUID,
    body: str,
    audience: str,
) -> dict[str, object]:
    normalized_audience = audience.strip().lower()
    if normalized_audience not in VALID_AUDIENCE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"audience must be one of: {sorted(VALID_AUDIENCE)}",
        )

    try:
        post_row = db.execute(
            insert(posts)
            .values(
                author_id=current_user_id,
                body=body.strip(),
                audience=normalized_audience,
            )
            .returning(
                posts.c.id,
                posts.c.author_id,
                posts.c.body,
                posts.c.audience,
                posts.c.vote_count,
                posts.c.comment_count,
                posts.c.created_at,
                posts.c.updated_at,
            )
        ).mappings().one()
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-post",
            metadata={"post_id": str(post_row["id"]), "audience": normalized_audience},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create post") from exc

    return {"post": _serialize_post(post_row)}


def get_post_by_id(db: Session, post_id: UUID, current_user_id: UUID | None = None) -> dict[str, object]:
    row = db.execute(
        select(
            posts.c.id, posts.c.author_id, posts.c.body, posts.c.audience,
            posts.c.vote_count, posts.c.comment_count, posts.c.created_at, posts.c.updated_at,
            users.c.username.label("author_username"),
            users.c.profile_image_url.label("author_profile_image_url"),
        )
        .select_from(posts.outerjoin(users, users.c.id == posts.c.author_id))
        .where(posts.c.id == post_id)
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    active_vote = 0
    if current_user_id is not None:
        vote_row = db.execute(
            select(content_votes.c.direction).where(
                content_votes.c.target_type == "post",
                content_votes.c.target_id == row["id"],
                content_votes.c.voter_id == current_user_id,
            )
        ).first()
        if vote_row is not None:
            active_vote = int(vote_row[0])

    comments_result = get_comments(db, subject_type="post", subject_id=row["id"], current_user_id=current_user_id)
    discussion = _attach_usernames_to_comments(db, comments_result["items"])

    return {
        "post": {
            "id": row["id"],
            "author_id": row["author_id"],
            "author_username": row["author_username"] or "",
            "author_profile_image_url": row["author_profile_image_url"],
            "body": row["body"],
            "audience": row["audience"],
            "vote_count": row["vote_count"],
            "comment_count": row["comment_count"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "active_vote": active_vote,
            "discussion": discussion,
        }
    }


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



def _load_help_request_roles(
    db: Session,
    help_request_ids: list[UUID],
    current_user_id: UUID | None = None,
) -> dict[str, list[dict[str, object]]]:
    if not help_request_ids:
        return {}

    filled_counts = dict(
        db.execute(
            select(
                help_request_role_assignments.c.role_id,
                func.count(help_request_role_assignments.c.user_id),
            )
            .where(
                help_request_role_assignments.c.role_id.in_(
                    select(help_request_roles.c.id).where(
                        help_request_roles.c.help_request_id.in_(help_request_ids)
                    )
                )
            )
            .group_by(help_request_role_assignments.c.role_id)
        ).all()
    )

    viewer_assignments: dict[UUID, UUID] = {}
    if current_user_id is not None:
        viewer_rows = db.execute(
            select(
                help_request_role_assignments.c.role_id,
                help_request_roles.c.help_request_id,
            )
            .select_from(
                help_request_role_assignments.join(
                    help_request_roles,
                    help_request_roles.c.id == help_request_role_assignments.c.role_id,
                )
            )
            .where(
                help_request_roles.c.help_request_id.in_(help_request_ids),
                help_request_role_assignments.c.user_id == current_user_id,
            )
        ).all()
        viewer_assignments = {hr_id: role_id for role_id, hr_id in viewer_rows}

    role_rows = db.execute(
        select(
            help_request_roles.c.id,
            help_request_roles.c.help_request_id,
            help_request_roles.c.title,
            help_request_roles.c.description,
            help_request_roles.c.slots,
            help_request_roles.c.sort_order,
        )
        .where(help_request_roles.c.help_request_id.in_(help_request_ids))
        .order_by(help_request_roles.c.help_request_id, help_request_roles.c.sort_order.asc())
    ).mappings().all()

    result: dict[str, list[dict[str, object]]] = {}
    for row in role_rows:
        hr_id = str(row["help_request_id"])
        role_id = row["id"]
        filled_count = int(filled_counts.get(role_id, 0))
        result.setdefault(hr_id, []).append(
            {
                "role_id": role_id,
                "title": row["title"],
                "description": row["description"],
                "slots": int(row["slots"]),
                "filled_count": filled_count,
                "is_viewer_assigned": viewer_assignments.get(row["help_request_id"]) == role_id,
            }
        )
    return result


def _help_request_role_summaries(
    roles: list[dict[str, object]],
) -> tuple[int, int]:
    signed_up = sum(int(role.get("filled_count", 0)) for role in roles)
    needed = sum(int(role.get("slots", 0)) for role in roles)
    return signed_up, needed


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
        value = value.replace(tzinfo=timezone.utc)
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
        needed_at = needed_at.replace(tzinfo=timezone.utc)

    channel_ids = _resolve_channel_ids(db, channel_slugs)
    community_ids = _resolve_community_ids(db, community_slugs, current_user_id)
    validated_roles = _validate_help_request_roles(roles)
    schedule_label = _format_needed_at_label(needed_at)
    try:
        created = db.execute(
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
        ).mappings().one()

        inserted_roles = []
        for index, role in enumerate(validated_roles):
            role_row = db.execute(
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
            ).mappings().one()
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
    row = db.execute(
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
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Help request not found")
    roles = _load_help_request_roles(db, [help_request_id], current_user_id).get(str(help_request_id), [])

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
    help_request_row = db.execute(
        select(
            help_requests.c.id,
            help_requests.c.author_id,
            help_requests.c.title,
        ).where(help_requests.c.id == help_request_id)
    ).mappings().first()
    if help_request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Help request not found")

    role_row = db.execute(
        select(
            help_request_roles.c.id,
            help_request_roles.c.help_request_id,
            help_request_roles.c.title,
            help_request_roles.c.slots,
        ).where(
            help_request_roles.c.id == role_id,
            help_request_roles.c.help_request_id == help_request_id,
        )
    ).mappings().first()
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
