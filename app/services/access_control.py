from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app.models import (
    comments,
    communities,
    event_memberships,
    event_tags,
    events,
    help_request_tags,
    help_requests,
    posts,
    project_tags,
    projects,
    scope_memberships,
    thread_tags,
    threads,
    user_follows,
)

COMMUNITY_SCOPE_KIND = "community"
CHANNEL_SCOPE_KIND = "channel"

TAGGED_ENTITY_TYPES = frozenset({"thread", "project", "event", "help_request"})
COMMENTABLE_SUBJECT_TYPES = frozenset({"thread", "post", "event", "project", "help_request"})
VOTE_TARGET_TYPES = frozenset({"thread", "post", "comment", "event", "project", "help_request"})

_TAG_TABLE_BY_ENTITY = {
    "thread": thread_tags,
    "project": project_tags,
    "event": event_tags,
    "help_request": help_request_tags,
}

_ENTITY_ID_COLUMN = {
    "thread": thread_tags.c.thread_id,
    "project": project_tags.c.project_id,
    "event": event_tags.c.event_id,
    "help_request": help_request_tags.c.help_request_id,
}


def _not_found(entity_type: str) -> HTTPException:
    label = entity_type.replace("_", " ").capitalize()
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{label} not found")


def is_scope_member(db: Session, scope_kind: str, scope_id: UUID, user_id: UUID) -> bool:
    row = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == scope_kind,
            scope_memberships.c.scope_id == scope_id,
            scope_memberships.c.user_id == user_id,
        )
    ).first()
    return row is not None


def viewer_follows_author(db: Session, viewer_id: UUID, author_id: UUID) -> bool:
    if viewer_id == author_id:
        return True
    row = db.execute(
        select(user_follows.c.follower_id).where(
            user_follows.c.follower_id == viewer_id,
            user_follows.c.followed_id == author_id,
            user_follows.c.status == "accepted",
        )
    ).first()
    return row is not None


def _entity_tag_scope(
    db: Session, entity_type: str, entity_id: UUID
) -> tuple[bool, bool, list[UUID]]:
    """Return (has_channel_tag, has_open_community_tag, closed_community_ids)."""
    tag_table = _TAG_TABLE_BY_ENTITY.get(entity_type)
    entity_col = _ENTITY_ID_COLUMN.get(entity_type)
    if tag_table is None or entity_col is None:
        return False, False, []

    rows = (
        db.execute(
            select(
                tag_table.c.channel_id,
                tag_table.c.community_id,
                communities.c.join_policy,
            )
            .select_from(
                tag_table.outerjoin(communities, communities.c.id == tag_table.c.community_id)
            )
            .where(entity_col == entity_id)
        )
        .mappings()
        .all()
    )

    has_channel_tag = False
    has_open_community_tag = False
    closed_community_ids: list[UUID] = []

    for row in rows:
        if row["channel_id"] is not None:
            has_channel_tag = True
        if row["community_id"] is not None:
            if row["join_policy"] == "closed":
                closed_community_ids.append(row["community_id"])
            else:
                has_open_community_tag = True

    return has_channel_tag, has_open_community_tag, closed_community_ids


def _viewer_is_member_of_communities(
    db: Session, viewer_id: UUID, community_ids: Sequence[UUID]
) -> bool:
    if not community_ids:
        return True
    membership_rows = db.execute(
        select(scope_memberships.c.scope_id).where(
            scope_memberships.c.scope_kind == COMMUNITY_SCOPE_KIND,
            scope_memberships.c.scope_id.in_(list(community_ids)),
            scope_memberships.c.user_id == viewer_id,
        )
    ).all()
    member_ids = {row[0] for row in membership_rows}
    return all(community_id in member_ids for community_id in community_ids)


def can_view_by_tags(
    db: Session, viewer_id: UUID | None, entity_type: str, entity_id: UUID
) -> bool:
    has_channel_tag, has_open_community_tag, closed_community_ids = _entity_tag_scope(
        db, entity_type, entity_id
    )
    if not closed_community_ids or has_channel_tag or has_open_community_tag:
        return True
    if viewer_id is None:
        return False
    return _viewer_is_member_of_communities(db, viewer_id, closed_community_ids)


def _event_is_private(db: Session, event_id: UUID) -> bool:
    row = db.execute(select(events.c.is_private).where(events.c.id == event_id)).first()
    return bool(row[0]) if row is not None else False


def _viewer_is_event_member(db: Session, viewer_id: UUID, event_id: UUID) -> bool:
    row = db.execute(
        select(event_memberships.c.user_id).where(
            event_memberships.c.event_id == event_id,
            event_memberships.c.user_id == viewer_id,
        )
    ).first()
    return row is not None


def can_view_post(db: Session, viewer_id: UUID | None, post_row: Mapping[str, object]) -> bool:
    audience = str(post_row.get("audience") or "public").lower()
    author_id = post_row.get("author_id")
    if audience != "followers":
        return True
    if viewer_id is None or author_id is None:
        return False
    return viewer_follows_author(db, viewer_id, author_id)


def can_view_entity(db: Session, viewer_id: UUID | None, entity_type: str, entity_id: UUID) -> bool:
    normalized = entity_type.strip().lower()
    if normalized == "post":
        row = (
            db.execute(
                select(posts.c.id, posts.c.author_id, posts.c.audience).where(
                    posts.c.id == entity_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        return can_view_post(db, viewer_id, row)

    if normalized == "event":
        row = (
            db.execute(select(events.c.id, events.c.is_private).where(events.c.id == entity_id))
            .mappings()
            .first()
        )
        if row is None:
            return False
        if row["is_private"]:
            return viewer_id is not None and _viewer_is_event_member(db, viewer_id, entity_id)
        return can_view_by_tags(db, viewer_id, normalized, entity_id)

    if normalized in TAGGED_ENTITY_TYPES:
        table = {
            "thread": threads,
            "project": projects,
            "event": events,
            "help_request": help_requests,
        }[normalized]
        if db.execute(select(table.c.id).where(table.c.id == entity_id)).first() is None:
            return False
        return can_view_by_tags(db, viewer_id, normalized, entity_id)

    return False


def assert_can_view_entity(
    db: Session,
    viewer_id: UUID | None,
    entity_type: str,
    entity_id: UUID,
) -> None:
    if not can_view_entity(db, viewer_id, entity_type, entity_id):
        raise _not_found(entity_type)


def assert_can_view_subject(
    db: Session,
    viewer_id: UUID | None,
    subject_type: str,
    subject_id: UUID,
) -> None:
    normalized = subject_type.strip().lower()
    if normalized not in COMMENTABLE_SUBJECT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"subject_type must be one of: {sorted(COMMENTABLE_SUBJECT_TYPES)}",
        )
    assert_can_view_entity(db, viewer_id, normalized, subject_id)


def assert_can_view_vote_target(
    db: Session,
    viewer_id: UUID | None,
    target_type: str,
    target_id: UUID,
) -> None:
    normalized = target_type.strip().lower()
    if normalized not in VOTE_TARGET_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"target_type must be one of: {sorted(VOTE_TARGET_TYPES)}",
        )
    if normalized == "comment":
        row = (
            db.execute(
                select(comments.c.subject_type, comments.c.subject_id).where(
                    comments.c.id == target_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
        assert_can_view_subject(db, viewer_id, str(row["subject_type"]), row["subject_id"])
        return
    assert_can_view_entity(db, viewer_id, normalized, target_id)


def assert_can_view_scope(
    db: Session,
    viewer_id: UUID | None,
    scope_kind: str,
    scope_id: UUID,
) -> None:
    normalized_kind = scope_kind.strip().lower()
    if normalized_kind == CHANNEL_SCOPE_KIND:
        return

    if normalized_kind != COMMUNITY_SCOPE_KIND:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid scope kind"
        )

    row = db.execute(select(communities.c.join_policy).where(communities.c.id == scope_id)).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Community not found")

    if row[0] != "closed":
        return

    if viewer_id is None or not is_scope_member(db, COMMUNITY_SCOPE_KIND, scope_id, viewer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Community not found")


def closed_community_only_tag_condition(tag_table, entity_id_column, entity_fk_name: str):
    """True when an entity is tagged only to closed communities (no public-scope escape hatch)."""
    entity_fk = getattr(tag_table.c, entity_fk_name)
    has_closed_community_tag = exists(
        select(tag_table.c.id)
        .select_from(tag_table.join(communities, communities.c.id == tag_table.c.community_id))
        .where(
            entity_fk == entity_id_column,
            tag_table.c.community_id.is_not(None),
            communities.c.join_policy == "closed",
        )
    )
    has_public_scope_tag = exists(
        select(tag_table.c.id)
        .select_from(tag_table.outerjoin(communities, communities.c.id == tag_table.c.community_id))
        .where(
            entity_fk == entity_id_column,
            or_(
                tag_table.c.channel_id.is_not(None),
                and_(
                    tag_table.c.community_id.is_not(None),
                    communities.c.join_policy != "closed",
                ),
            ),
        )
    )
    return and_(has_closed_community_tag, ~has_public_scope_tag)


def filter_search_results(
    db: Session,
    viewer_id: UUID | None,
    items: list[dict[str, object]],
) -> list[dict[str, object]]:
    filtered: list[dict[str, object]] = []
    for item in items:
        entity_type = str(item.get("entity_type") or "").lower()
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, UUID):
            continue
        if entity_type == "community":
            row = (
                db.execute(
                    select(communities.c.id, communities.c.join_policy).where(
                        communities.c.id == entity_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                continue
            if row["join_policy"] == "closed":
                if viewer_id is None or not is_scope_member(
                    db, COMMUNITY_SCOPE_KIND, entity_id, viewer_id
                ):
                    continue
            filtered.append(item)
            continue
        if entity_type == "user":
            filtered.append(item)
            continue
        if entity_type in TAGGED_ENTITY_TYPES or entity_type == "post":
            if can_view_entity(db, viewer_id, entity_type, entity_id):
                filtered.append(item)
            continue
        filtered.append(item)
    return filtered
