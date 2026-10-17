"""Direct invites to private communities and events wait for the person to accept.

Inviting creates a pending invite and a notification; nobody joins until they accept.
Invite links (scope_invites) and co-organizers chosen at event creation still join directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.models import (
    communities,
    event_memberships,
    events,
    pending_invites,
    scope_memberships,
    users,
)
from app.services.notifications import create_notification

COMMUNITY = "community"
EVENT = "event"


def _is_member(db: Session, kind: str, scope_id: UUID, user_id: UUID) -> bool:
    if kind == COMMUNITY:
        query = select(scope_memberships.c.user_id).where(
            scope_memberships.c.scope_kind == COMMUNITY,
            scope_memberships.c.scope_id == scope_id,
            scope_memberships.c.user_id == user_id,
        )
    else:
        query = select(event_memberships.c.user_id).where(
            event_memberships.c.event_id == scope_id,
            event_memberships.c.user_id == user_id,
        )
    return db.execute(query).first() is not None


def create_invite(
    db: Session,
    *,
    kind: str,
    scope_id: UUID,
    user_id: UUID,
    invited_by: UUID,
    title: str,
) -> bool:
    """Invite someone; returns False when they're already a member or already invited.

    Commits, then notifies the invitee with a link to accept or decline.
    """
    if _is_member(db, kind, scope_id, user_id):
        return False
    already = db.execute(
        select(pending_invites.c.id).where(
            pending_invites.c.scope_kind == kind,
            pending_invites.c.scope_id == scope_id,
            pending_invites.c.user_id == user_id,
            pending_invites.c.status == "pending",
        )
    ).first()
    if already is not None:
        return False
    db.execute(
        insert(pending_invites).values(
            scope_kind=kind, scope_id=scope_id, user_id=user_id, invited_by=invited_by
        )
    )
    db.commit()
    create_notification(
        db=db,
        recipient_id=user_id,
        actor_id=invited_by,
        kind="community-invite" if kind == COMMUNITY else "evt-invite",
        surface="community" if kind == COMMUNITY else "event",
        subject_type=kind,
        subject_id=scope_id,
        target_id=scope_id,
        title=title,
        body=(
            f"You were invited to join {title}. Accept or decline it in Invites."
            if kind == COMMUNITY
            else f"You were invited to an event: {title}. Accept or decline it in Invites."
        ),
        href="/invites",
    )
    return True


def pending_invite_count(db: Session, user_id: UUID) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(pending_invites)
            .where(pending_invites.c.user_id == user_id, pending_invites.c.status == "pending")
        ).scalar_one()
        or 0
    )


def list_pending_invites(db: Session, user_id: UUID) -> list[dict[str, object]]:
    rows = (
        db.execute(
            select(pending_invites)
            .where(pending_invites.c.user_id == user_id, pending_invites.c.status == "pending")
            .order_by(pending_invites.c.created_at.desc())
        )
        .mappings()
        .all()
    )
    if not rows:
        return []
    community_ids = [row["scope_id"] for row in rows if row["scope_kind"] == COMMUNITY]
    event_ids = [row["scope_id"] for row in rows if row["scope_kind"] == EVENT]
    community_rows = (
        {
            row["id"]: row
            for row in db.execute(
                select(
                    communities.c.id,
                    communities.c.slug,
                    communities.c.name,
                    communities.c.description,
                ).where(communities.c.id.in_(community_ids))
            ).mappings()
        }
        if community_ids
        else {}
    )
    event_rows = (
        {
            row["id"]: row
            for row in db.execute(
                select(
                    events.c.id,
                    events.c.slug,
                    events.c.title,
                    events.c.description,
                    events.c.scheduled_at,
                    events.c.ends_at,
                ).where(events.c.id.in_(event_ids))
            ).mappings()
        }
        if event_ids
        else {}
    )
    inviter_ids = {row["invited_by"] for row in rows if row["invited_by"]}
    inviters = (
        dict(
            db.execute(
                select(users.c.id, users.c.username).where(users.c.id.in_(list(inviter_ids)))
            ).all()
        )
        if inviter_ids
        else {}
    )
    items: list[dict[str, object]] = []
    for row in rows:
        if row["scope_kind"] == COMMUNITY:
            scope = community_rows.get(row["scope_id"])
            if scope is None:
                continue
            item = {
                "title": scope["name"],
                "description": scope["description"],
                "href": f"/communities/{scope['slug']}",
                "scheduled_at": None,
                "ends_at": None,
            }
        else:
            scope = event_rows.get(row["scope_id"])
            if scope is None:
                continue
            item = {
                "title": scope["title"],
                "description": scope["description"],
                "href": f"/events/{scope['slug']}",
                "scheduled_at": scope["scheduled_at"].isoformat()
                if scope["scheduled_at"]
                else None,
                "ends_at": scope["ends_at"].isoformat() if scope["ends_at"] else None,
            }
        items.append(
            {
                "id": str(row["id"]),
                "kind": row["scope_kind"],
                "invited_by": inviters.get(row["invited_by"]),
                "created_at": row["created_at"].isoformat(),
                **item,
            }
        )
    return items


def respond_to_invite(
    db: Session, user_id: UUID, invite_id: UUID, accept: bool
) -> dict[str, object]:
    invite = (
        db.execute(select(pending_invites).where(pending_invites.c.id == invite_id))
        .mappings()
        .first()
    )
    if invite is None or invite["user_id"] != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    if invite["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You've already answered this invite"
        )
    now = datetime.now(UTC)
    href = None
    if accept and not _is_member(db, invite["scope_kind"], invite["scope_id"], user_id):
        if invite["scope_kind"] == COMMUNITY:
            db.execute(
                insert(scope_memberships).values(
                    scope_kind=COMMUNITY,
                    scope_id=invite["scope_id"],
                    user_id=user_id,
                    role="member",
                )
            )
        else:
            db.execute(
                insert(event_memberships).values(
                    event_id=invite["scope_id"], user_id=user_id, role="member", joined_at=now
                )
            )
            db.execute(
                update(events)
                .where(events.c.id == invite["scope_id"])
                .values(member_count=events.c.member_count + 1)
            )
    if accept:
        if invite["scope_kind"] == COMMUNITY:
            slug = db.execute(
                select(communities.c.slug).where(communities.c.id == invite["scope_id"])
            ).scalar_one_or_none()
            href = f"/communities/{slug}" if slug else None
        else:
            slug = db.execute(
                select(events.c.slug).where(events.c.id == invite["scope_id"])
            ).scalar_one_or_none()
            href = f"/events/{slug}" if slug else None
    db.execute(
        update(pending_invites)
        .where(pending_invites.c.id == invite_id)
        .values(status="accepted" if accept else "declined", responded_at=now)
    )
    db.commit()
    return {"ok": True, "accepted": accept, "href": href}
