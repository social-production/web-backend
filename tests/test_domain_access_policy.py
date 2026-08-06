"""Unit tests for portable access-policy helpers."""

from __future__ import annotations

from uuid import uuid4

from app.domain.access_policy import (
    EntityTagScope,
    can_view_by_tag_scope,
    is_closed_community_only_scope,
)


def test_public_via_channel_tag() -> None:
    scope = EntityTagScope(
        has_channel_tag=True,
        has_open_community_tag=False,
        closed_community_ids=(uuid4(),),
    )
    assert can_view_by_tag_scope(
        viewer_id=None,
        scope=scope,
        viewer_closed_community_memberships=[],
    )


def test_closed_community_requires_membership() -> None:
    community_id = uuid4()
    scope = EntityTagScope(
        has_channel_tag=False,
        has_open_community_tag=False,
        closed_community_ids=(community_id,),
    )
    assert not can_view_by_tag_scope(
        viewer_id=None,
        scope=scope,
        viewer_closed_community_memberships=[],
    )
    assert not can_view_by_tag_scope(
        viewer_id=uuid4(),
        scope=scope,
        viewer_closed_community_memberships=[],
    )
    assert can_view_by_tag_scope(
        viewer_id=uuid4(),
        scope=scope,
        viewer_closed_community_memberships=[community_id],
    )


def test_untagged_is_public() -> None:
    scope = EntityTagScope(
        has_channel_tag=False,
        has_open_community_tag=False,
        closed_community_ids=(),
    )
    assert can_view_by_tag_scope(
        viewer_id=None,
        scope=scope,
        viewer_closed_community_memberships=[],
    )
    assert is_closed_community_only_scope(EntityTagScope(False, False, (uuid4(),)))
    assert not is_closed_community_only_scope(scope)
