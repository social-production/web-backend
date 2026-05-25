from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, func, not_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    conversation_members,
    conversations,
    notifications,
    scope_memberships,
    user_follows,
    users,
)


def get_onboarding(db: Session) -> dict[str, object]:
    channel_names = db.execute(
        select(channels.c.name).order_by(channels.c.name.asc()).limit(8)
    ).scalars().all()
    community_names = db.execute(
        select(communities.c.name).order_by(communities.c.name.asc()).limit(8)
    ).scalars().all()

    return {
        "title": "Signup / Login",
        "intro": "Anonymous visitors can read public surfaces first. To post, follow people, or open create flows, sign up or log in.",
        "accountModes": [
            {
                "value": "signup",
                "label": "Sign up",
                "description": "Start a fresh username and local profile.",
            },
            {
                "value": "login",
                "label": "Log in",
                "description": "Use an existing account once authentication is wired.",
            },
        ],
        "starterChannels": list(channel_names),
        "starterCommunities": list(community_names),
    }


def _get_viewer_row(db: Session, current_user_id: UUID):
    row = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url).where(
            users.c.id == current_user_id
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Viewer not found")
    return row


def _get_unread_notification_count(db: Session, current_user_id: UUID) -> int:
    count = db.execute(
        select(func.count())
        .select_from(notifications)
        .where(
            notifications.c.recipient_id == current_user_id,
            notifications.c.is_unread.is_(True),
        )
    ).scalar_one()
    return int(count or 0)


def _get_unread_message_count(db: Session, current_user_id: UUID) -> int:
    count = db.execute(
        select(func.count())
        .select_from(
            conversation_members.join(
                conversations,
                conversations.c.id == conversation_members.c.conversation_id,
            )
        )
        .where(
            conversation_members.c.user_id == current_user_id,
            conversations.c.last_message_at.is_not(None),
            or_(
                conversation_members.c.last_read_at.is_(None),
                conversations.c.last_message_at > conversation_members.c.last_read_at,
            ),
        )
    ).scalar_one()
    return int(count or 0)


def _get_platform_directory_item(db: Session) -> dict[str, object] | None:
    row = db.execute(
        select(channels.c.slug, channels.c.name)
        .where(channels.c.slug.in_(["platform", "stewardship"]))
        .order_by(channels.c.slug.asc())
        .limit(1)
    ).mappings().first()
    if row is None:
        return None
    return {
        "slug": row["slug"],
        "label": row["name"],
        "href": "/platform",
        "visibility": "public",
    }


def _get_channel_directory_items(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(channels.c.slug, channels.c.name)
        .select_from(
            scope_memberships.join(channels, channels.c.id == scope_memberships.c.scope_id)
        )
        .where(
            scope_memberships.c.user_id == current_user_id,
            scope_memberships.c.scope_kind == "channel",
        )
        .order_by(channels.c.name.asc())
    ).mappings().all()

    return [
        {
            "slug": row["slug"],
            "label": row["name"],
            "href": f"/channels/{row['slug']}",
            "visibility": "public",
        }
        for row in rows
    ]


def _get_community_directory_items(db: Session, current_user_id: UUID) -> list[dict[str, object]]:
    rows = db.execute(
        select(communities.c.slug, communities.c.name, communities.c.join_policy)
        .select_from(
            scope_memberships.join(communities, communities.c.id == scope_memberships.c.scope_id)
        )
        .where(
            scope_memberships.c.user_id == current_user_id,
            scope_memberships.c.scope_kind == "community",
        )
        .order_by(communities.c.name.asc())
    ).mappings().all()

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

    rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url)
        .where(
            users.c.is_active.is_(True),
            users.c.id != current_user_id,
            not_(users.c.id.in_(select(followed_subquery.c.followed_id))),
        )
        .order_by(users.c.username.asc())
        .limit(8)
    ).mappings().all()

    return [
        {
            "id": row["id"],
            "username": row["username"],
            "bio": row["bio"],
            "profileImageUrl": row["profile_image_url"],
        }
        for row in rows
    ]


def get_bootstrap(db: Session, current_user_id: UUID) -> dict[str, object]:
    viewer = _get_viewer_row(db, current_user_id)

    return {
        "viewer": {
            "id": viewer["id"],
            "username": viewer["username"],
            "bio": viewer["bio"],
            "profileImageUrl": viewer["profile_image_url"],
        },
        "featureFlags": {
            "assets": False,
            "funding": False,
            "platform": True,
        },
        "unreadCounts": {
            "notifications": _get_unread_notification_count(db, current_user_id),
            "messages": _get_unread_message_count(db, current_user_id),
        },
        "directory": {
            "platform": _get_platform_directory_item(db),
            "channels": _get_channel_directory_items(db, current_user_id),
            "communities": _get_community_directory_items(db, current_user_id),
        },
        "suggestedContacts": _get_suggested_contacts(db, current_user_id),
        "activityRail": [],
    }
