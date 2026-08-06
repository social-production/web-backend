"""Pure access-policy helpers — no SQL / Session.

These encode the visibility rules that SQL adapters and future providers must
preserve. Keep decision math here; keep query assembly in access_control /
Postgres adapters.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class EntityTagScope:
    """Normalized tag scope for a tagged entity (thread/project/event/help)."""

    has_channel_tag: bool
    has_open_community_tag: bool
    closed_community_ids: tuple[UUID, ...]


def can_view_by_tag_scope(
    *,
    viewer_id: UUID | None,
    scope: EntityTagScope,
    viewer_closed_community_memberships: Sequence[UUID],
) -> bool:
    """Apply public-escape-hatch rules for tagged entities.

    Visibility:
    - Channel tags or open community tags make the entity publicly viewable.
    - Closed-community-only tags require membership in every closed community.
    - No tags → treated as public (caller may still apply entity-specific rules).
    """
    if scope.has_channel_tag or scope.has_open_community_tag:
        return True

    if not scope.closed_community_ids:
        return True

    if viewer_id is None:
        return False

    member_ids = set(viewer_closed_community_memberships)
    return all(community_id in member_ids for community_id in scope.closed_community_ids)


def is_closed_community_only_scope(scope: EntityTagScope) -> bool:
    """True when the entity is tagged only to closed communities."""
    return (
        bool(scope.closed_community_ids)
        and not scope.has_channel_tag
        and not scope.has_open_community_tag
    )


def normalize_entity_type(entity_type: str) -> str:
    return entity_type.strip().lower()
