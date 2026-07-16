from __future__ import annotations

from uuid import UUID

from sqlalchemy import not_, select
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    scope_memberships,
    user_follows,
    users,
)


def _get_platform_directory_item(
    db: Session, current_user_id: UUID | None
) -> dict[str, object] | None:
    row = (
        db.execute(
            select(channels.c.id, channels.c.slug, channels.c.name)
            .where(channels.c.slug.in_(["platform", "stewardship"]))
            .order_by(channels.c.slug.asc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    if row is None:
        return None

    membership = None
    if current_user_id is not None:
        membership = db.execute(
            select(scope_memberships.c.user_id).where(
                scope_memberships.c.scope_kind == "channel",
                scope_memberships.c.scope_id == row["id"],
                scope_memberships.c.user_id == current_user_id,
            )
        ).first()

    return {
        "slug": row["slug"],
        "label": row["name"],
        "href": "/platform",
        "visibility": "public",
        "viewerIsMember": membership is not None,
    }


def _get_channel_directory_items(
    db: Session, current_user_id: UUID | None
) -> list[dict[str, object]]:
    if current_user_id is None:
        return []

    rows = (
        db.execute(
            select(channels.c.slug, channels.c.name)
            .select_from(
                scope_memberships.join(channels, channels.c.id == scope_memberships.c.scope_id)
            )
            .where(
                scope_memberships.c.user_id == current_user_id,
                scope_memberships.c.scope_kind == "channel",
                channels.c.slug.not_in(["platform", "stewardship"]),
            )
            .order_by(channels.c.name.asc())
        )
        .mappings()
        .all()
    )

    return [
        {
            "slug": row["slug"],
            "label": row["name"],
            "href": f"/channels/{row['slug']}",
            "visibility": "public",
        }
        for row in rows
    ]


def _get_community_directory_items(
    db: Session, current_user_id: UUID | None
) -> list[dict[str, object]]:
    if current_user_id is None:
        return []

    rows = (
        db.execute(
            select(communities.c.slug, communities.c.name, communities.c.join_policy)
            .select_from(
                scope_memberships.join(
                    communities, communities.c.id == scope_memberships.c.scope_id
                )
            )
            .where(
                scope_memberships.c.user_id == current_user_id,
                scope_memberships.c.scope_kind == "community",
            )
            .order_by(communities.c.name.asc())
        )
        .mappings()
        .all()
    )

    return [
        {
            "slug": row["slug"],
            "label": row["name"],
            "href": f"/communities/{row['slug']}",
            "visibility": "private" if row["join_policy"] == "closed" else "public",
        }
        for row in rows
    ]


def _get_suggested_contacts(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    followed_subquery = (
        select(user_follows.c.followed_id)
        .where(
            user_follows.c.follower_id == current_user_id,
            user_follows.c.status == "accepted",
        )
        .subquery("followed_users")
    )

    rows = (
        db.execute(
            select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url)
            .where(
                users.c.is_active.is_(True),
                users.c.id != current_user_id,
                not_(users.c.id.in_(select(followed_subquery.c.followed_id))),
            )
            .order_by(users.c.username.asc())
            .limit(8)
        )
        .mappings()
        .all()
    )

    return [
        {
            "id": row["id"],
            "username": row["username"],
            "bio": row["bio"],
            "profileImageUrl": row["profile_image_url"],
        }
        for row in rows
    ]
