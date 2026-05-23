from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import user_follows, user_settings, users


USER_SETTINGS_FIELDS = {
    "appearance_theme_mode",
    "default_feed",
    "public_feed_scope",
    "public_feed_filter",
    "public_feed_sort",
    "public_feed_window",
    "personal_feed_scope",
    "personal_feed_filter",
    "personal_feed_sort",
    "personal_feed_window",
    "hide_public_activity_from_personal_feeds",
    "hide_personal_feed_from_non_followers",
    "require_follow_approval",
}


USER_PROFILE_FIELDS = {"bio", "profile_image_url"}


def _serialize_user(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "username": row["username"],
        "bio": row["bio"],
        "profile_image_url": row["profile_image_url"],
        "is_active": row["is_active"],
    }


def _serialize_settings(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "appearance_theme_mode": row["appearance_theme_mode"],
        "default_feed": row["default_feed"],
        "public_feed_scope": row["public_feed_scope"],
        "public_feed_filter": row["public_feed_filter"],
        "public_feed_sort": row["public_feed_sort"],
        "public_feed_window": row["public_feed_window"],
        "personal_feed_scope": row["personal_feed_scope"],
        "personal_feed_filter": row["personal_feed_filter"],
        "personal_feed_sort": row["personal_feed_sort"],
        "personal_feed_window": row["personal_feed_window"],
        "hide_public_activity_from_personal_feeds": row["hide_public_activity_from_personal_feeds"],
        "hide_personal_feed_from_non_followers": row["hide_personal_feed_from_non_followers"],
        "require_follow_approval": row["require_follow_approval"],
    }


def _get_user_by_username(db: Session, username: str) -> Mapping[str, object]:
    row = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url, users.c.is_active).where(users.c.username == username.lower())
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return row


def _get_settings_for_user(db: Session, user_id: UUID) -> Mapping[str, object]:
    row = db.execute(select(user_settings).where(user_settings.c.user_id == user_id)).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User settings not found")
    return row


def get_profile_by_username(db: Session, username: str) -> dict[str, object]:
    user_row = _get_user_by_username(db, username)
    return {"user": _serialize_user(user_row)}


def get_own_profile(db: Session, current_user_id: UUID) -> dict[str, object]:
    user_row = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url, users.c.is_active).where(users.c.id == current_user_id)
    ).mappings().first()
    if user_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    settings_row = _get_settings_for_user(db, current_user_id)
    return {"user": _serialize_user(user_row), "settings": _serialize_settings(settings_row)}


def update_own_profile_settings(db: Session, current_user_id: UUID, payload: dict[str, object]) -> dict[str, object]:
    profile_updates = {k: v for k, v in payload.items() if k in USER_PROFILE_FIELDS}
    settings_updates = {k: v for k, v in payload.items() if k in USER_SETTINGS_FIELDS}

    if not profile_updates and not settings_updates:
        return get_own_profile(db, current_user_id)

    if profile_updates:
        db.execute(update(users).where(users.c.id == current_user_id).values(**profile_updates))

    if settings_updates:
        db.execute(
            update(user_settings)
            .where(user_settings.c.user_id == current_user_id)
            .values(**settings_updates)
        )

    db.commit()
    return get_own_profile(db, current_user_id)


def follow_user(db: Session, current_user_id: UUID, username: str) -> dict[str, object]:
    target_user = _get_user_by_username(db, username)
    target_user_id = target_user["id"]

    if target_user_id == current_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot follow yourself")

    try:
        db.execute(
            insert(user_follows).values(
                follower_id=current_user_id,
                followed_id=target_user_id,
                status="accepted",
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()

    return {"ok": True, "following": True, "username": target_user["username"]}


def unfollow_user(db: Session, current_user_id: UUID, username: str) -> dict[str, object]:
    target_user = _get_user_by_username(db, username)
    target_user_id = target_user["id"]

    db.execute(
        delete(user_follows).where(
            and_(
                user_follows.c.follower_id == current_user_id,
                user_follows.c.followed_id == target_user_id,
            )
        )
    )
    db.commit()
    return {"ok": True, "following": False, "username": target_user["username"]}


def get_followers(db: Session, username: str) -> dict[str, object]:
    target_user = _get_user_by_username(db, username)

    follower_users = users.alias("follower_users")
    rows = db.execute(
        select(
            follower_users.c.id,
            follower_users.c.username,
            follower_users.c.bio,
            follower_users.c.profile_image_url,
            follower_users.c.is_active,
            user_follows.c.status,
        )
        .select_from(
            user_follows.join(follower_users, user_follows.c.follower_id == follower_users.c.id)
        )
        .where(user_follows.c.followed_id == target_user["id"])
        .order_by(follower_users.c.username.asc())
    ).mappings().all()

    items = [
        {
            **_serialize_user(row),
            "follow_status": row["status"],
        }
        for row in rows
    ]
    return {"items": items, "total": len(items), "username": target_user["username"]}


def get_following(db: Session, username: str) -> dict[str, object]:
    target_user = _get_user_by_username(db, username)

    followed_users = users.alias("followed_users")
    rows = db.execute(
        select(
            followed_users.c.id,
            followed_users.c.username,
            followed_users.c.bio,
            followed_users.c.profile_image_url,
            followed_users.c.is_active,
            user_follows.c.status,
        )
        .select_from(
            user_follows.join(followed_users, user_follows.c.followed_id == followed_users.c.id)
        )
        .where(user_follows.c.follower_id == target_user["id"])
        .order_by(followed_users.c.username.asc())
    ).mappings().all()

    items = [
        {
            **_serialize_user(row),
            "follow_status": row["status"],
        }
        for row in rows
    ]
    return {"items": items, "total": len(items), "username": target_user["username"]}
