from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import users
from app.services.bootstrap.activity_rail import _build_activity_rail
from app.services.bootstrap.activity_rail_history import _build_activity_rail_history
from app.services.bootstrap.directory import (
    _get_channel_directory_items,
    _get_community_directory_items,
    _get_platform_directory_item,
    _get_suggested_contacts,
)
from app.services.bootstrap.summary import (
    _get_viewer_row,
    get_bootstrap_summary,
)
from app.services.standing import paused_ids, standing_for


def _paused_usernames(db: Session) -> list[str]:
    """Accounts a bot review found to be bots, so posts and comments can carry a Paused label."""
    ids = paused_ids(db)
    if not ids:
        return []
    return sorted(db.execute(select(users.c.username).where(users.c.id.in_(ids))).scalars())


def get_bootstrap(db: Session, current_user_id: UUID | None) -> dict[str, object]:
    if current_user_id is None:
        return {
            "viewer": None,
            "featureFlags": {
                "assets": False,
                "funding": False,
                "platform": True,
            },
            "unreadCounts": get_bootstrap_summary(db, None)["unreadCounts"],
            "directory": {
                "platform": _get_platform_directory_item(db, None),
                "channels": [],
                "communities": [],
            },
            "suggestedContacts": [],
            "activityRail": [],
            "activityRailHistory": [],
            "pausedUsernames": _paused_usernames(db),
        }

    viewer = _get_viewer_row(db, current_user_id)

    return {
        "viewer": {
            "id": viewer["id"],
            "username": viewer["username"],
            "bio": viewer["bio"],
            "profileImageUrl": viewer["profile_image_url"],
            # "paused" means a bot review was upheld: the account is read-only.
            "standing": standing_for(db, current_user_id).level,
        },
        "featureFlags": {
            "assets": False,
            "funding": False,
            "platform": True,
        },
        "unreadCounts": get_bootstrap_summary(db, current_user_id)["unreadCounts"],
        "directory": {
            "platform": _get_platform_directory_item(db, current_user_id),
            "channels": _get_channel_directory_items(db, current_user_id),
            "communities": _get_community_directory_items(db, current_user_id),
        },
        "suggestedContacts": _get_suggested_contacts(db, current_user_id),
        "activityRail": _build_activity_rail(db, current_user_id),
        "activityRailHistory": _build_activity_rail_history(db, current_user_id),
        "pausedUsernames": _paused_usernames(db),
    }
