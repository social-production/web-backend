from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import channels, communities, content_votes, posts, scope_memberships, thread_tags, threads, users
from app.services.governance import get_comments
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
