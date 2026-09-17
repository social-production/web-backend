from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import user_settings

NOTIFICATION_CATEGORIES = (
    "follows",
    "comments",
    "shares_invites",
    "roles",
    "votes_needed",
    "phase_done",
    "plan_leading",
)

DEFAULT_NOTIFICATION_CATEGORIES = (
    "follows",
    "comments",
    "shares_invites",
    "roles",
    "votes_needed",
    "phase_done",
)

KIND_TO_CATEGORY: dict[str, str] = {
    "follow-request": "follows",
    "new-follower": "follows",
    "follow-accepted": "follows",
    "reply": "comments",
    "mention": "comments",
    "prj-share": "shares_invites",
    "evt-share": "shares_invites",
    "evt-invite": "shares_invites",
    "community-invite": "shares_invites",
    "hr-role-signup": "roles",
    "prj-role-suggest": "roles",
    "evt-role-suggest": "roles",
    "prj-phase-vote": "votes_needed",
    "evt-phase-vote": "votes_needed",
    "pr-approved": "votes_needed",
    "prj-phase-done": "phase_done",
    "evt-phase-done": "phase_done",
    "prj-plan-lead": "plan_leading",
    "evt-plan-lead": "plan_leading",
}


def category_for_kind(kind: str) -> str | None:
    return KIND_TO_CATEGORY.get(kind.strip())


def normalize_notification_categories(value: object | None) -> list[str]:
    raw: Sequence[object]
    if value is None:
        raw = DEFAULT_NOTIFICATION_CATEGORIES
    elif isinstance(value, str):
        raw = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = DEFAULT_NOTIFICATION_CATEGORIES

    seen: set[str] = set()
    categories: list[str] = []
    for item in raw:
        key = str(item).strip()
        if key not in NOTIFICATION_CATEGORIES or key in seen:
            continue
        seen.add(key)
        categories.append(key)
    return categories


def enabled_notification_categories(settings_row: Mapping[str, object] | None) -> set[str]:
    if settings_row is None:
        return set(DEFAULT_NOTIFICATION_CATEGORIES)
    return set(normalize_notification_categories(settings_row.get("notification_categories")))


def allowed_notification_kinds(settings_row: Mapping[str, object] | None) -> list[str]:
    enabled = enabled_notification_categories(settings_row)
    return [kind for kind, category in KIND_TO_CATEGORY.items() if category in enabled]


def recipient_allows_notification(db: Session, recipient_id: UUID, kind: str) -> bool:
    category = category_for_kind(kind)
    if category is None:
        return False
    row = (
        db.execute(select(user_settings).where(user_settings.c.user_id == recipient_id))
        .mappings()
        .first()
    )
    return category in enabled_notification_categories(row)
