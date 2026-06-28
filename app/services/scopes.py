from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from urllib.parse import unquote
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import channels, communities, scope_invites, scope_memberships, users
from app.services.meaningful_actions import record_meaningful_action
from app.services.notifications import create_notification
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


def _slug_in_use(db: Session, slug: str) -> bool:
    normalized = slug.strip().lower()
    if not normalized:
        return False
    channel_exists = db.execute(select(channels.c.id).where(channels.c.slug == normalized)).first()
    community_exists = db.execute(select(communities.c.id).where(communities.c.slug == normalized)).first()
    return channel_exists is not None or community_exists is not None


def _name_in_use(db: Session, name: str) -> bool:
    normalized = name.strip().lower()
    if not normalized:
        return False
    channel_exists = db.execute(
        select(channels.c.id).where(func.lower(channels.c.name) == normalized)
    ).first()
    community_exists = db.execute(
        select(communities.c.id).where(func.lower(communities.c.name) == normalized)
    ).first()
    return channel_exists is not None or community_exists is not None


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


def _is_scope_manager(db: Session, scope_kind: str, scope_id: UUID, user_id: UUID) -> bool:
    row = db.execute(
        select(scope_memberships.c.role).where(
            scope_memberships.c.scope_kind == scope_kind,
            scope_memberships.c.scope_id == scope_id,
            scope_memberships.c.user_id == user_id,
        )
    ).first()
    return row is not None and row[0] == "manager"


def _is_scope_member(db: Session, scope_kind: str, scope_id: UUID, user_id: UUID) -> bool:
    row = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == scope_kind,
            scope_memberships.c.scope_id == scope_id,
            scope_memberships.c.user_id == user_id,
        )
    ).first()
    return row is not None


def _can_create_scope_invite(
    db: Session,
    scope_kind: str,
    scope_row: Mapping[str, object],
    user_id: UUID,
) -> bool:
    if not _is_scope_member(db, scope_kind, scope_row["id"], user_id):
        return False
    if scope_kind == COMMUNITY_SCOPE_KIND and scope_row["join_policy"] == "closed":
        return True
    return _is_scope_manager(db, scope_kind, scope_row["id"], user_id)


def _community_invite_url(slug: str, token: str) -> str:
    return f"/communities/{slug}?invite={token}"


def _invite_redeem_url(scope_kind: str, slug: str, token: str) -> str:
    if scope_kind == COMMUNITY_SCOPE_KIND:
        return _community_invite_url(slug, token)
    return f"/scopes/invites/redeem?token={token}"


def normalize_invite_token(raw: str) -> str:
    value = raw.strip()
    if not value:
        return value

    for param in ("invite", "token"):
        match = re.search(rf"[?&]{param}=([^&#]+)", value)
        if match:
            return unquote(match.group(1).strip())

    return value


def create_scope_invite(
    db: Session,
    current_user_id: UUID,
    scope_kind: str,
    slug: str,
) -> dict[str, object]:
    scope_row = _get_scope_row(db, scope_kind, slug)
    if not _can_create_scope_invite(db, scope_kind, scope_row, current_user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only community members can create invites")

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)

    try:
        db.execute(
            insert(scope_invites).values(
                scope_kind=scope_kind,
                scope_id=scope_row["id"],
                token_hash=token_hash,
                created_by=current_user_id,
                expires_at=now + timedelta(days=30),
                max_uses=None,
                uses=0,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not create invite") from exc

    redeem_url = _invite_redeem_url(scope_kind, scope_row["slug"], token)
    return {"token": token, "redeem_url": redeem_url}


def list_taggable_scopes(
    db: Session,
    current_user_id: UUID,
    query: str = "",
    kind: str | None = None,
    limit: int = 8,
) -> dict[str, object]:
    normalized_query = query.strip().lower()
    normalized_kind = kind.strip().lower() if kind else None
    capped_limit = max(1, min(limit, 25))
    channel_items: list[dict[str, object]] = []
    community_items: list[dict[str, object]] = []

    if normalized_kind in (None, CHANNEL_SCOPE_KIND):
        channel_conditions = []
        if normalized_query:
            channel_conditions.append(
                or_(
                    channels.c.slug.ilike(f"%{normalized_query}%"),
                    channels.c.name.ilike(f"%{normalized_query}%"),
                )
            )

        channel_rows = db.execute(
            select(
                channels.c.id,
                channels.c.slug,
                channels.c.name,
                scope_memberships.c.user_id.label("member_user_id"),
            )
            .select_from(
                channels.outerjoin(
                    scope_memberships,
                    and_(
                        scope_memberships.c.scope_kind == CHANNEL_SCOPE_KIND,
                        scope_memberships.c.scope_id == channels.c.id,
                        scope_memberships.c.user_id == current_user_id,
                    ),
                )
            )
            .where(*channel_conditions)
            .order_by(channels.c.name.asc())
            .limit(capped_limit)
        ).mappings().all()

        channel_items = [
            {
                "slug": row["slug"],
                "label": row["name"],
                "href": f"/channels/{row['slug']}",
                "visibility": "public",
                "viewer_is_member": row["member_user_id"] is not None,
            }
            for row in channel_rows
        ]

    if normalized_kind in (None, COMMUNITY_SCOPE_KIND):
        community_conditions = [
            or_(
                communities.c.join_policy != "closed",
                scope_memberships.c.user_id.is_not(None),
            )
        ]
        if normalized_query:
            community_conditions.append(
                or_(
                    communities.c.slug.ilike(f"%{normalized_query}%"),
                    communities.c.name.ilike(f"%{normalized_query}%"),
                )
            )

        community_rows = db.execute(
            select(
                communities.c.id,
                communities.c.slug,
                communities.c.name,
                communities.c.join_policy,
                scope_memberships.c.user_id.label("member_user_id"),
            )
            .select_from(
                communities.outerjoin(
                    scope_memberships,
                    and_(
                        scope_memberships.c.scope_kind == COMMUNITY_SCOPE_KIND,
                        scope_memberships.c.scope_id == communities.c.id,
                        scope_memberships.c.user_id == current_user_id,
                    ),
                )
            )
            .where(*community_conditions)
            .order_by(communities.c.name.asc())
            .limit(capped_limit)
        ).mappings().all()

        community_items = [
            {
                "slug": row["slug"],
                "label": row["name"],
                "href": f"/communities/{row['slug']}",
                "visibility": "private" if row["join_policy"] == "closed" else "public",
                "viewer_is_member": row["member_user_id"] is not None,
            }
            for row in community_rows
        ]

    return {"channels": channel_items, "communities": community_items}


def create_channel(db: Session, current_user_id: UUID, slug: str, name: str, description: str) -> dict[str, object]:
    normalized_slug = slug.strip().lower()
    normalized_name = name.strip()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")
    if not normalized_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Name is required")
    if _slug_in_use(db, normalized_slug):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That slug is already used by a channel or community",
        )
    if _name_in_use(db, normalized_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That name is already used by a channel or community",
        )

    try:
        created_row = db.execute(
            insert(channels)
            .values(
                slug=normalized_slug,
                name=normalized_name,
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
        db.execute(
            insert(scope_memberships).values(
                scope_kind=CHANNEL_SCOPE_KIND,
                scope_id=created_row["id"],
                user_id=current_user_id,
                role="manager",
            )
        )
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
    normalized_name = name.strip()
    normalized_join_policy = join_policy.strip().lower()
    if not normalized_slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Slug is required")
    if not normalized_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Name is required")
    if _slug_in_use(db, normalized_slug):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That slug is already used by a channel or community",
        )
    if _name_in_use(db, normalized_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That name is already used by a channel or community",
        )

    try:
        created_row = db.execute(
            insert(communities)
            .values(
                slug=normalized_slug,
                name=normalized_name,
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
        db.execute(
            insert(scope_memberships).values(
                scope_kind=COMMUNITY_SCOPE_KIND,
                scope_id=created_row["id"],
                user_id=current_user_id,
                role="manager",
            )
        )
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


def get_channel_by_slug(db: Session, slug: str, current_user_id: UUID | None = None) -> dict[str, object]:
    row = _get_channel_row(db, slug)
    member_rows = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == CHANNEL_SCOPE_KIND,
            scope_memberships.c.scope_id == row["id"],
        )
    ).all()
    viewer_is_member = False
    if current_user_id is not None:
        viewer_is_member = any(str(r[0]) == str(current_user_id) for r in member_rows)
    return {
        "channel": _serialize_channel(row),
        "member_count": len(member_rows),
        "viewer_is_member": viewer_is_member,
    }


def get_community_by_slug(db: Session, slug: str, current_user_id: UUID | None = None) -> dict[str, object]:
    row = _get_community_row(db, slug)
    member_rows = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == COMMUNITY_SCOPE_KIND,
            scope_memberships.c.scope_id == row["id"],
        )
    ).all()
    viewer_is_member = False
    if current_user_id is not None:
        viewer_is_member = any(str(r[0]) == str(current_user_id) for r in member_rows)

    result: dict[str, object] = {
        "community": _serialize_community(row),
        "member_count": len(member_rows),
        "viewer_is_member": viewer_is_member,
    }
    return result


def join_scope(db: Session, current_user_id: UUID, scope_kind: str, slug: str) -> dict[str, object]:
    scope_row = _get_scope_row(db, scope_kind, slug)
    if scope_kind == COMMUNITY_SCOPE_KIND and scope_row["join_policy"] == "closed":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Community is closed")

    # Check if already a member before inserting
    existing = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == scope_kind,
            scope_memberships.c.scope_id == scope_row["id"],
            scope_memberships.c.user_id == current_user_id,
        )
    ).first()
    if existing is not None:
        return {"ok": True, "joined": False, "scope_kind": scope_kind, "slug": scope_row["slug"], "detail": "Already a member"}

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
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not join scope")

    # Record the meaningful action AFTER the commit, so it can't roll back the membership
    try:
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="join-scope",
            metadata={"scope_kind": scope_kind, "scope_id": str(scope_row["id"]), "slug": slug},
        )
        db.commit()
    except Exception:
        db.rollback()
        pass

    return {"ok": True, "joined": True, "scope_kind": scope_kind, "slug": scope_row["slug"]}


def leave_scope(db: Session, current_user_id: UUID, scope_kind: str, slug: str) -> dict[str, object]:
    scope_row = _get_scope_row(db, scope_kind, slug)
    try:
        db.execute(
            delete(scope_memberships).where(
                scope_memberships.c.scope_kind == scope_kind,
                scope_memberships.c.scope_id == scope_row["id"],
                scope_memberships.c.user_id == current_user_id,
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not leave scope") from exc

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


def redeem_scope_invite(db: Session, current_user_id: UUID, token: str) -> dict[str, object]:
    normalized_token = normalize_invite_token(token)
    if not normalized_token:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="token is required")

    token_hash = hashlib.sha256(normalized_token.encode("utf-8")).hexdigest()
    invite = db.execute(
        select(scope_invites).where(scope_invites.c.token_hash == token_hash)
    ).mappings().first()
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")

    now = datetime.now(timezone.utc)
    if invite["expires_at"] is not None and invite["expires_at"] < now:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite has expired")
    if invite["max_uses"] is not None and int(invite["uses"] or 0) >= int(invite["max_uses"]):
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invite has no uses remaining")

    scope_kind = invite["scope_kind"]
    if scope_kind == CHANNEL_SCOPE_KIND:
        scope_row = db.execute(select(channels).where(channels.c.id == invite["scope_id"])).mappings().first()
    elif scope_kind == COMMUNITY_SCOPE_KIND:
        scope_row = db.execute(select(communities).where(communities.c.id == invite["scope_id"])).mappings().first()
    else:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invite scope kind unsupported")

    if scope_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite scope not found")

    existing = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == scope_kind,
            scope_memberships.c.scope_id == scope_row["id"],
            scope_memberships.c.user_id == current_user_id,
        )
    ).first()
    if existing is not None:
        return {"ok": True, "joined": False, "scope_kind": scope_kind, "slug": scope_row["slug"]}

    try:
        db.execute(
            insert(scope_memberships).values(
                scope_kind=scope_kind,
                scope_id=scope_row["id"],
                user_id=current_user_id,
                role="member",
            )
        )

        db.execute(
            update(scope_invites)
            .where(scope_invites.c.id == invite["id"])
            .values(uses=int(invite["uses"] or 0) + 1)
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not redeem invite") from exc

    return {"ok": True, "joined": True, "scope_kind": scope_kind, "slug": scope_row["slug"]}


def invite_user_to_community(
    db: Session,
    current_user_id: UUID,
    slug: str,
    username: str,
) -> dict[str, object]:
    community_row = _get_community_row(db, slug)
    if community_row["join_policy"] != "closed":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Direct invites are only available for private communities",
        )
    if not _is_scope_member(db, COMMUNITY_SCOPE_KIND, community_row["id"], current_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only community members can invite others",
        )

    normalized_username = username.strip()
    if not normalized_username:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="username is required")

    target_user = db.execute(
        select(users.c.id, users.c.username).where(users.c.username == normalized_username)
    ).mappings().first()
    if target_user is None or target_user["id"] == current_user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    existing = db.execute(
        select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == COMMUNITY_SCOPE_KIND,
            scope_memberships.c.scope_id == community_row["id"],
            scope_memberships.c.user_id == target_user["id"],
        )
    ).first()
    if existing is not None:
        return {"ok": True, "username": target_user["username"], "already_member": True}

    try:
        db.execute(
            insert(scope_memberships).values(
                scope_kind=COMMUNITY_SCOPE_KIND,
                scope_id=community_row["id"],
                user_id=target_user["id"],
                role="member",
            )
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not invite user",
        ) from exc

    create_notification(
        db=db,
        recipient_id=target_user["id"],
        actor_id=current_user_id,
        kind="community-invite",
        surface="community",
        subject_type="community",
        subject_id=community_row["id"],
        target_id=community_row["id"],
        title=community_row["name"],
        body="You were invited to join this private community.",
        href=f"/communities/{community_row['slug']}",
    )

    return {"ok": True, "username": target_user["username"], "already_member": False}
