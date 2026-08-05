from __future__ import annotations

from app.services.moderation.electorates import _hybrid_engaged_size


def test_hybrid_engaged_size_uses_participants_over_members() -> None:
    assert (
        _hybrid_engaged_size(
            member_population=1,
            participant_count=3,
            voter_count=0,
            public_surface=True,
        )
        == 3
    )


def test_hybrid_engaged_size_applies_public_floor_when_engaged() -> None:
    assert (
        _hybrid_engaged_size(
            member_population=2,
            participant_count=2,
            voter_count=0,
            public_surface=True,
        )
        == 3
    )


def test_hybrid_engaged_size_keeps_solo_author_small() -> None:
    assert (
        _hybrid_engaged_size(
            member_population=1,
            participant_count=1,
            voter_count=0,
            public_surface=True,
        )
        == 1
    )
