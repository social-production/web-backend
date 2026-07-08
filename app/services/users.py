from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from datetime import datetime, timezone
from zoneinfo import available_timezones

from app.models import notifications, user_follows, user_settings, users
from app.services.meaningful_actions import record_meaningful_action


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
    "hide_public_profile_activity_from_non_followers",
    "require_follow_approval",
    "preferred_language",
    "display_timezone",
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
        "hide_public_profile_activity_from_non_followers": row[
            "hide_public_profile_activity_from_non_followers"
        ],
        "require_follow_approval": row["require_follow_approval"],
        "preferred_language": row["preferred_language"],
        "display_timezone": row["display_timezone"],
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


def _get_follow_status(
    db: Session,
    follower_id: UUID,
    followed_id: UUID,
) -> str | None:
    row = db.execute(
        select(user_follows.c.status).where(
            user_follows.c.follower_id == follower_id,
            user_follows.c.followed_id == followed_id,
        )
    ).first()
    return row[0] if row else None


def get_profile_by_username(
    db: Session,
    username: str,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    user_row = _get_user_by_username(db, username)
    viewer_is_following = False
    viewer_follow_status: str | None = None
    is_own_profile = current_user_id == user_row["id"]

    if current_user_id is not None and not is_own_profile:
        viewer_follow_status = _get_follow_status(db, current_user_id, user_row["id"])
        viewer_is_following = viewer_follow_status == "accepted"

    settings_row = _get_settings_for_user(db, user_row["id"])
    hide_personal_feed = bool(settings_row["hide_personal_feed_from_non_followers"])
    hide_public_profile = bool(settings_row["hide_public_profile_activity_from_non_followers"])
    can_view_personal_feed = is_own_profile or viewer_is_following or not hide_personal_feed
    can_view_public_profile_activity = (
        is_own_profile
        or viewer_is_following
        or not hide_public_profile
    )

    return {
        "user": _serialize_user(user_row),
        "viewer_is_following": viewer_is_following,
        "viewer_follow_status": viewer_follow_status,
        "is_own_profile": is_own_profile,
        "can_view_personal_feed": can_view_personal_feed,
        "can_view_public_profile_activity": can_view_public_profile_activity,
    }


def get_own_profile(db: Session, current_user_id: UUID) -> dict[str, object]:
    user_row = db.execute(
        select(users.c.id, users.c.username, users.c.bio, users.c.profile_image_url, users.c.is_active).where(users.c.id == current_user_id)
    ).mappings().first()
    if user_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    settings_row = _get_settings_for_user(db, current_user_id)
    return {"user": _serialize_user(user_row), "settings": _serialize_settings(settings_row)}


VALID_LANGUAGES = {"en", "nl"}


def update_own_profile_settings(db: Session, current_user_id: UUID, payload: dict[str, object]) -> dict[str, object]:
    profile_updates = {k: v for k, v in payload.items() if k in USER_PROFILE_FIELDS}
    settings_updates = {k: v for k, v in payload.items() if k in USER_SETTINGS_FIELDS}

    if "preferred_language" in settings_updates:
        language = str(settings_updates["preferred_language"]).strip().lower()
        if language not in VALID_LANGUAGES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_preferred_language")
        settings_updates["preferred_language"] = language

    if "display_timezone" in settings_updates:
        timezone_value = settings_updates["display_timezone"]
        if timezone_value is None or str(timezone_value).strip() == "":
            settings_updates["display_timezone"] = None
        else:
            timezone_name = str(timezone_value).strip()
            if timezone_name not in available_timezones():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="invalid_display_timezone",
                )
            settings_updates["display_timezone"] = timezone_name

    if not profile_updates and not settings_updates:
        return get_own_profile(db, current_user_id)

    try:
        if profile_updates:
            db.execute(update(users).where(users.c.id == current_user_id).values(**profile_updates))

        if settings_updates:
            db.execute(
                update(user_settings)
                .where(user_settings.c.user_id == current_user_id)
                .values(**settings_updates)
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not update profile settings") from exc

    return get_own_profile(db, current_user_id)


def follow_user(db: Session, current_user_id: UUID, username: str) -> dict[str, object]:
    target_user = _get_user_by_username(db, username)
    target_user_id = target_user["id"]

    if target_user_id == current_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot follow yourself")

    existing_status = _get_follow_status(db, current_user_id, target_user_id)
    if existing_status is not None:
        return {
            "ok": True,
            "following": existing_status == "accepted",
            "follow_status": existing_status,
            "username": target_user["username"],
        }

    target_settings = _get_settings_for_user(db, target_user_id)
    follow_status = "pending" if bool(target_settings["require_follow_approval"]) else "accepted"
    created_new = False

    try:
        db.execute(
            insert(user_follows).values(
                follower_id=current_user_id,
                followed_id=target_user_id,
                status=follow_status,
            )
        )
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="follow-user",
            metadata={
                "target_user_id": str(target_user_id),
                "target_username": target_user["username"],
                "follow_status": follow_status,
            },
        )
        db.commit()
        created_new = True
    except IntegrityError:
        db.rollback()
        existing_status = _get_follow_status(db, current_user_id, target_user_id)
        if existing_status is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not update follow status",
            ) from None
        follow_status = existing_status

    if created_new and follow_status in ("pending", "accepted"):
        from app.services.notifications import create_notification

        actor_row = db.execute(
            select(users.c.username).where(users.c.id == current_user_id)
        ).first()
        actor_username = actor_row[0] if actor_row else "someone"
        try:
            if follow_status == "pending":
                create_notification(
                    db=db,
                    recipient_id=target_user_id,
                    actor_id=current_user_id,
                    kind="follow-request",
                    surface="profile",
                    subject_type="user",
                    subject_id=current_user_id,
                    target_id=target_user_id,
                    title="Follow request",
                    body=f"@{actor_username} requested to follow you.",
                    href=f"/profile/{actor_username}",
                )
            else:
                create_notification(
                    db=db,
                    recipient_id=target_user_id,
                    actor_id=current_user_id,
                    kind="new-follower",
                    surface="profile",
                    subject_type="user",
                    subject_id=current_user_id,
                    target_id=target_user_id,
                    title="New follower",
                    body=f"@{actor_username} started following you.",
                    href=f"/profile/{actor_username}",
                )
        except HTTPException:
            pass

    return {
        "ok": True,
        "following": follow_status == "accepted",
        "follow_status": follow_status,
        "username": target_user["username"],
    }


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
    return {"ok": True, "following": False, "follow_status": None, "username": target_user["username"]}


def get_followers(
    db: Session,
    username: str,
    viewer_user_id: UUID | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, object]:
    target_user = _get_user_by_username(db, username)
    is_own_profile = viewer_user_id is not None and viewer_user_id == target_user["id"]
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)

    follower_users = users.alias("follower_users")
    query = (
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
    )
    if not is_own_profile:
        query = query.where(user_follows.c.status == "accepted")

    rows = db.execute(
        query.order_by(follower_users.c.username.asc()).limit(safe_limit).offset(safe_offset)
    ).mappings().all()

    items = [
        {
            **_serialize_user(row),
            "follow_status": row["status"],
        }
        for row in rows
    ]
    accepted_total = db.execute(
        select(func.count())
        .select_from(user_follows)
        .where(
            user_follows.c.followed_id == target_user["id"],
            user_follows.c.status == "accepted",
        )
    ).scalar_one()
    return {
        "items": items,
        "total": int(accepted_total or 0),
        "limit": safe_limit,
        "offset": safe_offset,
        "username": target_user["username"],
    }


def get_following(
    db: Session,
    username: str,
    viewer_user_id: UUID | None = None,
    *,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, object]:
    target_user = _get_user_by_username(db, username)
    is_own_profile = viewer_user_id is not None and viewer_user_id == target_user["id"]
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)

    followed_users = users.alias("followed_users")
    query = (
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
    )
    if not is_own_profile:
        query = query.where(user_follows.c.status == "accepted")

    rows = db.execute(
        query.order_by(followed_users.c.username.asc()).limit(safe_limit).offset(safe_offset)
    ).mappings().all()

    items = [
        {
            **_serialize_user(row),
            "follow_status": row["status"],
        }
        for row in rows
    ]
    accepted_total = db.execute(
        select(func.count())
        .select_from(user_follows)
        .where(
            user_follows.c.follower_id == target_user["id"],
            user_follows.c.status == "accepted",
        )
    ).scalar_one()
    return {
        "items": items,
        "total": int(accepted_total or 0),
        "limit": safe_limit,
        "offset": safe_offset,
        "username": target_user["username"],
    }


def get_follow_requests(db: Session, current_user_id: UUID) -> dict[str, object]:
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
        .where(
            user_follows.c.followed_id == current_user_id,
            user_follows.c.status == "pending",
        )
        .order_by(follower_users.c.username.asc())
    ).mappings().all()

    items = [{**_serialize_user(row), "follow_status": row["status"]} for row in rows]
    return {"items": items, "total": len(items)}


def _resolve_follow_request_notification(
    db: Session,
    recipient_id: UUID,
    follower_id: UUID,
    follower_username: str,
    *,
    accepted: bool,
) -> None:
    body = (
        f"You accepted @{follower_username}'s follow request."
        if accepted
        else f"You declined @{follower_username}'s follow request."
    )
    db.execute(
        update(notifications)
        .where(
            notifications.c.recipient_id == recipient_id,
            notifications.c.actor_id == follower_id,
            notifications.c.kind == "follow-request",
            notifications.c.is_unread.is_(True),
        )
        .values(
            is_unread=False,
            read_at=datetime.now(timezone.utc),
            body=body,
        )
    )


def accept_follow_request(db: Session, current_user_id: UUID, follower_username: str) -> dict[str, object]:
    follower_user = _get_user_by_username(db, follower_username)
    result = db.execute(
        update(user_follows)
        .where(
            user_follows.c.follower_id == follower_user["id"],
            user_follows.c.followed_id == current_user_id,
            user_follows.c.status == "pending",
        )
        .values(status="accepted")
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Follow request not found")
    _resolve_follow_request_notification(
        db,
        current_user_id,
        follower_user["id"],
        follower_user["username"],
        accepted=True,
    )

    accepter_row = db.execute(
        select(users.c.username).where(users.c.id == current_user_id)
    ).first()
    accepter_username = accepter_row[0] if accepter_row else "someone"

    from app.services.notifications import create_notification

    try:
        create_notification(
            db=db,
            recipient_id=follower_user["id"],
            actor_id=current_user_id,
            kind="follow-accepted",
            surface="profile",
            subject_type="user",
            subject_id=current_user_id,
            target_id=follower_user["id"],
            title="Follow request accepted",
            body=f"@{accepter_username} accepted your follow request.",
            href=f"/profile/{accepter_username}",
        )
    except HTTPException:
        pass

    db.commit()
    return {"ok": True, "username": follower_user["username"], "follow_status": "accepted"}


def reject_follow_request(db: Session, current_user_id: UUID, follower_username: str) -> dict[str, object]:
    follower_user = _get_user_by_username(db, follower_username)
    result = db.execute(
        delete(user_follows).where(
            user_follows.c.follower_id == follower_user["id"],
            user_follows.c.followed_id == current_user_id,
            user_follows.c.status == "pending",
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Follow request not found")
    _resolve_follow_request_notification(
        db,
        current_user_id,
        follower_user["id"],
        follower_user["username"],
        accepted=False,
    )
    db.commit()
    return {"ok": True, "username": follower_user["username"], "follow_status": None}
