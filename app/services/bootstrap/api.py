from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.services.bootstrap.activity_rail import _build_activity_rail
from app.services.bootstrap.activity_rail_history import _build_activity_rail_history
from app.services.bootstrap.directory import (
    _get_channel_directory_items,
    _get_community_directory_items,
    _get_platform_directory_item,
    _get_suggested_contacts,
)
from app.services.bootstrap.summary import (
    _get_unread_message_count,
    _get_unread_notification_count,
    _get_viewer_row,
)


def get_bootstrap(db: Session, current_user_id: UUID | None) -> dict[str, object]:
    if current_user_id is None:
        return {
            "viewer": None,
            "featureFlags": {
                "assets": False,
                "funding": False,
                "platform": True,
            },
            "unreadCounts": {
                "notifications": 0,
                "messages": 0,
            },
            "directory": {
                "platform": _get_platform_directory_item(db, None),
                "channels": [],
                "communities": [],
            },
            "suggestedContacts": [],
            "activityRail": [],
            "activityRailHistory": [],
        }

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
            "platform": _get_platform_directory_item(db, current_user_id),
            "channels": _get_channel_directory_items(db, current_user_id),
            "communities": _get_community_directory_items(db, current_user_id),
        },
        "suggestedContacts": _get_suggested_contacts(db, current_user_id),
        "activityRail": _build_activity_rail(db, current_user_id),
        "activityRailHistory": _build_activity_rail_history(db, current_user_id),
    }
