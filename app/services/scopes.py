from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import channels, communities, scope_memberships, users
from app.services.search import index_document

CHANNEL_SCOPE_KIND = "channel"
COMMUNITY_SCOPE_KIND = "community"


def _serialize_user(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "username": row["username"],
        "bio": row["bio"],
        "profile_image_url": row["profile_image_url"],
        "is_active": row["is_active"],
    }


def _serialize_channel(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _serialize_community(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "join_policy": row["join_policy"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_channel_row(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(
        select(channels).where(channels.c.slug == slug.lower())
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return row


def _get_community_row(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(
        select(communities).where(communities.c.slug == slug.lower())
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Community not found")
    return row


def _get_scope_row(db: Session, scope_kind: str, slug: str) -> Mapping[str, object]:
    if scope_kind == CHANNEL_SCOPE_KIND:
        return _get_channel_row(db, slug)
    if scope_kind == COMMUNITY_SCOPE_KIND:
        return _get_community_row(db, slug)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported scope kind")


def _scope_table(scope_kind: str):
    if scope_kind == CHANNEL_SCOPE_KIND:
        return channels
    if scope_kind == COMMUNITY_SCOPE_KIND:
        return communities
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported scope kind")


def _serialize_scope(scope_kind: str, row: Mapping[str, object]) -> dict[str, object]:
    if scope_kind == CHANNEL_SCOPE_KIND:
        return _serialize_channel(row)
    return _serialize_community(row)


def create_channel(db: Session, current_user_id: UUID, slug: str, name: str, description: str) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")

    try:
        created_row = db.execute(
            insert(channels)
            .values(
                slug=normalized_slug,
                name=name.strip(),
                description=description.strip(),
                created_by=current_user_id,
            )
            .returning(
                channels.c.id,
                channels.c.slug,
                channels.c.name,
                channels.c.description,
                channels.c.created_by,
                channels.c.created_at,
                channels.c.updated_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Channel slug already exists") from exc

    index_document(
        db=db,
        entity_type="channel",
        entity_id=created_row["id"],
        title=created_row["name"],
        summary=created_row["description"],
        meta="channel",
        href=f"/channels/{created_row['slug']}",
    )
    return {"channel": _serialize_channel(created_row)}


def create_community(db: Session, current_user_id: UUID, slug: str, name: str, description: str, join_policy: str = "open") -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    normalized_join_policy = join_policy.strip().lower()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")

    try:
        created_row = db.execute(
            insert(communities)
            .values(
                slug=normalized_slug,
                name=name.strip(),
                description=description.strip(),
                join_policy=normalized_join_policy,
                created_by=current_user_id,
            )
            .returning(
                communities.c.id,
                communities.c.slug,
                communities.c.name,
                communities.c.description,
                communities.c.join_policy,
                communities.c.created_by,
                communities.c.created_at,
                communities.c.updated_at,
            )
        ).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Community slug already exists") from exc

    index_document(
        db=db,
        entity_type="community",
        entity_id=created_row["id"],
        title=created_row["name"],
        summary=created_row["description"],
        meta="community",
        href=f"/communities/{created_row['slug']}",
    )
    return {"community": _serialize_community(created_row)}


def get_channel_by_slug(db: Session, slug: str) -> dict[str, object]:
    row = _get_channel_row(db, slug)
    member_count = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == CHANNEL_SCOPE_KIND,
            scope_memberships.c.scope_id == row["id"],
        )
    ).all()
    return {"channel": _serialize_channel(row), "member_count": len(member_count)}


def get_community_by_slug(db: Session, slug: str) -> dict[str, object]:
    row = _get_community_row(db, slug)
    member_count = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == COMMUNITY_SCOPE_KIND,
            scope_memberships.c.scope_id == row["id"],
        )
    ).all()
    return {"community": _serialize_community(row), "member_count": len(member_count)}


def join_scope(db: Session, current_user_id: UUID, scope_kind: str, slug: str) -> dict[str, object]:
    scope_row = _get_scope_row(db, scope_kind, slug)
    if scope_kind == COMMUNITY_SCOPE_KIND and scope_row["join_policy"] == "closed":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Community is closed")

    try:
        db.execute(
            insert(scope_memberships).values(
                scope_kind=scope_kind,
                scope_id=scope_row["id"],
                user_id=current_user_id,
                role="member",
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()

    return {"ok": True, "joined": True, "scope_kind": scope_kind, "slug": scope_row["slug"]}


def leave_scope(db: Session, current_user_id: UUID, scope_kind: str, slug: str) -> dict[str, object]:
    scope_row = _get_scope_row(db, scope_kind, slug)
    db.execute(
        delete(scope_memberships).where(
            scope_memberships.c.scope_kind == scope_kind,
            scope_memberships.c.scope_id == scope_row["id"],
            scope_memberships.c.user_id == current_user_id,
        )
    )
    db.commit()
    return {"ok": True, "joined": False, "scope_kind": scope_kind, "slug": scope_row["slug"]}


def list_scope_members(db: Session, scope_kind: str, slug: str) -> dict[str, object]:
    scope_row = _get_scope_row(db, scope_kind, slug)
    member_users = users.alias(f"{scope_kind}_members")
    rows = db.execute(
        select(
            member_users.c.id,
            member_users.c.username,
            member_users.c.bio,
            member_users.c.profile_image_url,
            member_users.c.is_active,
            scope_memberships.c.role,
            scope_memberships.c.created_at,
        )
        .select_from(
            scope_memberships.join(member_users, scope_memberships.c.user_id == member_users.c.id)
        )
        .where(
            scope_memberships.c.scope_kind == scope_kind,
            scope_memberships.c.scope_id == scope_row["id"],
        )
        .order_by(member_users.c.username.asc())
    ).mappings().all()

    items = [
        {
            **_serialize_user(row),
            "role": row["role"],
            "joined_at": row["created_at"],
        }
        for row in rows
    ]
    return {"scope_kind": scope_kind, "slug": scope_row["slug"], "total": len(items), "items": items}
