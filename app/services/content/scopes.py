from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    channels,
    communities,
    help_request_tags,
    scope_memberships,
)

VALID_AUDIENCE = frozenset({"public", "followers"})


def _get_help_request_tags_enriched(
    db: Session, help_request_id: UUID
) -> tuple[list[dict], list[dict]]:
    """Returns (channel_tags, community_tags) each as [{slug, label, kind}]."""
    rows = (
        db.execute(
            select(
                help_request_tags.c.tag_kind,
                channels.c.slug.label("channel_slug"),
                channels.c.name.label("channel_name"),
                communities.c.slug.label("community_slug"),
                communities.c.name.label("community_name"),
            )
            .select_from(help_request_tags)
            .outerjoin(channels, channels.c.id == help_request_tags.c.channel_id)
            .outerjoin(communities, communities.c.id == help_request_tags.c.community_id)
            .where(help_request_tags.c.help_request_id == help_request_id)
        )
        .mappings()
        .all()
    )

    channel_tags = [
        {"slug": r["channel_slug"], "label": r["channel_name"], "kind": "channel"}
        for r in rows
        if r["channel_slug"]
    ]
    community_tags = [
        {"slug": r["community_slug"], "label": r["community_name"], "kind": "community"}
        for r in rows
        if r["community_slug"]
    ]
    return channel_tags, community_tags


def _resolve_channel_ids(db: Session, channel_slugs: list[str]) -> list[UUID]:
    """Return the UUIDs for the given channel slugs, raising 404 for any unknown slug."""
    normalized = [s.strip().lower() for s in channel_slugs if s.strip()]
    if not normalized:
        return []
    rows = (
        db.execute(select(channels.c.id, channels.c.slug).where(channels.c.slug.in_(normalized)))
        .mappings()
        .all()
    )
    found_slugs = {row["slug"] for row in rows}
    missing = set(normalized) - found_slugs
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown channel slugs: {sorted(missing)}",
        )
    return [row["id"] for row in rows]


def _resolve_community_ids(
    db: Session, community_slugs: list[str], current_user_id: UUID
) -> list[UUID]:
    """Return the UUIDs for the given community slugs, raising 422 for any unknown slug."""
    normalized = [s.strip().lower() for s in community_slugs if s.strip()]
    if not normalized:
        return []
    rows = (
        db.execute(
            select(communities.c.id, communities.c.slug, communities.c.join_policy).where(
                communities.c.slug.in_(normalized)
            )
        )
        .mappings()
        .all()
    )
    found_slugs = {row["slug"] for row in rows}
    missing = set(normalized) - found_slugs
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown community slugs: {sorted(missing)}",
        )
    closed_ids = [row["id"] for row in rows if row["join_policy"] == "closed"]
    if closed_ids:
        membership_rows = db.execute(
            select(scope_memberships.c.scope_id).where(
                scope_memberships.c.scope_kind == "community",
                scope_memberships.c.scope_id.in_(closed_ids),
                scope_memberships.c.user_id == current_user_id,
            )
        ).all()
        member_ids = {row[0] for row in membership_rows}
        forbidden = sorted(
            row["slug"] for row in rows if row["id"] in closed_ids and row["id"] not in member_ids
        )
        if forbidden:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You must be a member to tag private communities: {forbidden}",
            )

    return [row["id"] for row in rows]
