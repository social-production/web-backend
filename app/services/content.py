from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import channels, posts, thread_tags, threads
from app.services.meaningful_actions import record_meaningful_action
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


def create_thread(
    db: Session,
    current_user_id: UUID,
    slug: str,
    title: str,
    body: str,
    channel_slugs: list[str],
) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")

    if not channel_slugs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Threads require at least one channel tag",
        )

    channel_ids = _resolve_channel_ids(db, channel_slugs)

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


def get_thread_by_slug(db: Session, slug: str) -> dict[str, object]:
    row = db.execute(
        select(threads).where(threads.c.slug == slug.lower())
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    tags = _get_thread_tags(db, row["id"])
    return {"thread": _serialize_thread(row, tags)}


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


def get_post_by_id(db: Session, post_id: UUID) -> dict[str, object]:
    row = db.execute(
        select(posts).where(posts.c.id == post_id)
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
    return {"post": _serialize_post(row)}
