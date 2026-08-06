"""Portable domain packages — storage-agnostic business rules."""

from app.domain.access_policy import (
    EntityTagScope,
    can_view_by_tag_scope,
    is_closed_community_only_scope,
    normalize_entity_type,
)

__all__ = [
    "EntityTagScope",
    "can_view_by_tag_scope",
    "is_closed_community_only_scope",
    "normalize_entity_type",
]
