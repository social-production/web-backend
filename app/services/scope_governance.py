"""Settings for channels and communities: picture, name, description, and who decides.

collaborative – any member proposes from the gear; members vote (66% yes). Quorum is based on the
                member count; the Platform channel uses the weekly active user base instead.
organizer     – private communities only. Organizers (role "manager") edit directly, add other
                organizers and remove members. Switching to collaborative needs 66% of organizers.
In a collaborative private community members can also vote to remove someone or to hand control
to named organizers.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.adapters.postgres.blob_store import PostgresBlobStore
from app.crypto.messages import decrypt_bytes, encrypt_bytes
from app.models import (
    channels,
    communities,
    scope_memberships,
    scope_proposal_votes,
    scope_proposals,
    users,
)
from app.services.access_control import assert_can_view_scope
from app.utils.votes import required_votes, weekly_active_users_global

APPROVAL_SHARE = 0.66
PROPOSAL_DAYS = 7
PROPOSAL_KINDS = frozenset({"edit", "remove_member", "to_collaborative", "to_organizer"})
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _plural(kind: str) -> str:
    return "communities" if kind == "community" else "channels"


def _table(kind: str):
    if kind == "channel":
        return channels
    if kind == "community":
        return communities
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported scope kind")


def _row(db: Session, kind: str, slug: str) -> Mapping[str, object]:
    table = _table(kind)
    row = db.execute(select(table).where(table.c.slug == slug)).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return row


def _is_private(kind: str, row: Mapping[str, object]) -> bool:
    return kind == "community" and row.get("join_policy") == "closed"


def _mode(kind: str, row: Mapping[str, object]) -> str:
    if _is_private(kind, row) and row.get("governance") == "organizer":
        return "organizer"
    return "collaborative"


def _is_platform(kind: str, row: Mapping[str, object]) -> bool:
    return kind == "channel" and str(row["slug"]).lower() == "platform"


def _memberships(db: Session, kind: str, scope_id: UUID) -> dict:
    rows = db.execute(
        select(scope_memberships.c.user_id, scope_memberships.c.role).where(
            scope_memberships.c.scope_kind == kind, scope_memberships.c.scope_id == scope_id
        )
    ).all()
    return {user_id: role for user_id, role in rows}


def image_url(kind: str, row: Mapping[str, object]) -> str | None:
    key = row.get("image_storage_key")
    if not key:
        return None
    return f"/scopes/{_plural(kind)}/{row['slug']}/image?v={str(key)[-8:]}"


# ---- images -------------------------------------------------------------------------------


def store_image(
    db: Session, viewer_id: UUID, kind: str, slug: str, content_type: str | None, data: bytes
) -> dict:
    row = _row(db, kind, slug)
    if viewer_id not in _memberships(db, kind, row["id"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Join to change the picture"
        )
    media = (content_type or "").split(";")[0].strip().lower()
    if media not in IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Use a JPEG, PNG or WebP picture",
        )
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Pictures can be up to 4 MB"
        )
    key = f"scope-image-{uuid4()}"
    PostgresBlobStore(db).put(key, encrypt_bytes(data))
    db.commit()
    return {"image_key": key, "content_type": media}


def read_image(
    db: Session, viewer_id: UUID | None, kind: str, slug: str, key: str | None
) -> tuple[bytes, str]:
    row = _row(db, kind, slug)
    if kind == "community":
        assert_can_view_scope(db, viewer_id, "community", row["id"])
    wanted = key or row.get("image_storage_key")
    content_type = row.get("image_content_type")
    if key and key != row.get("image_storage_key"):
        pending = db.execute(
            select(scope_proposals.c.payload).where(
                scope_proposals.c.scope_kind == kind,
                scope_proposals.c.scope_id == row["id"],
                scope_proposals.c.status == "open",
            )
        ).scalars()
        match = next((p for p in pending if (p or {}).get("image_key") == key), None)
        if match is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No picture")
        content_type = match.get("image_content_type")
    if not wanted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No picture")
    stored = PostgresBlobStore(db).get(str(wanted))
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No picture")
    return decrypt_bytes(stored), str(content_type or "image/jpeg")


# ---- proposals ----------------------------------------------------------------------------


def _electorate(db: Session, kind: str, row, proposal_kind: str) -> list:
    members = _memberships(db, kind, row["id"])
    if proposal_kind == "to_collaborative":
        return [user_id for user_id, role in members.items() if role == "manager"]
    return list(members)


def _tally(db: Session, kind: str, row, proposal) -> dict:
    electorate = set(_electorate(db, kind, row, proposal["kind"]))
    votes = db.execute(
        select(scope_proposal_votes.c.voter_id, scope_proposal_votes.c.vote).where(
            scope_proposal_votes.c.proposal_id == proposal["id"]
        )
    ).all()
    yes = sum(1 for voter, vote in votes if voter in electorate and vote == "yes")
    no = sum(1 for voter, vote in votes if voter in electorate and vote == "no")
    total = yes + no
    if proposal["kind"] == "to_collaborative":
        size = len(electorate)
        needed_yes = max(1, ceil(APPROVAL_SHARE * size))
        passing = yes >= needed_yes
        failed = no > size - needed_yes
        return {
            "yes": yes,
            "no": no,
            "votes_required": needed_yes,
            "passing": passing,
            "failed": failed,
        }
    population = weekly_active_users_global(db) if _is_platform(kind, row) else len(electorate)
    needed = max(1, required_votes(population))
    passing = total >= needed and yes / total >= APPROVAL_SHARE if total else False
    remaining = max(0, max(population, needed) - total)
    max_total = total + remaining
    can_pass = (
        max_total >= needed and (yes + remaining) / max_total >= APPROVAL_SHARE
        if max_total
        else False
    )
    return {
        "yes": yes,
        "no": no,
        "votes_required": needed,
        "passing": passing,
        "failed": not passing and not can_pass,
    }


def _apply(db: Session, kind: str, row, proposal) -> None:
    table = _table(kind)
    payload = proposal["payload"] or {}
    if proposal["kind"] == "edit":
        _apply_edit(db, kind, row, payload)
    elif proposal["kind"] == "remove_member":
        db.execute(
            delete(scope_memberships).where(
                scope_memberships.c.scope_kind == kind,
                scope_memberships.c.scope_id == row["id"],
                scope_memberships.c.user_id == UUID(str(payload["target_user_id"])),
            )
        )
    elif proposal["kind"] == "to_collaborative":
        db.execute(update(table).where(table.c.id == row["id"]).values(governance="collaborative"))
    elif proposal["kind"] == "to_organizer":
        organizer_ids = [UUID(str(value)) for value in payload.get("organizer_ids", [])]
        db.execute(update(table).where(table.c.id == row["id"]).values(governance="organizer"))
        db.execute(
            update(scope_memberships)
            .where(
                scope_memberships.c.scope_kind == kind, scope_memberships.c.scope_id == row["id"]
            )
            .values(role="member")
        )
        if organizer_ids:
            db.execute(
                update(scope_memberships)
                .where(
                    scope_memberships.c.scope_kind == kind,
                    scope_memberships.c.scope_id == row["id"],
                    scope_memberships.c.user_id.in_(organizer_ids),
                )
                .values(role="manager")
            )


def _apply_edit(db: Session, kind: str, row, payload: Mapping[str, object]) -> None:
    table = _table(kind)
    values: dict[str, object] = {}
    if payload.get("name"):
        values["name"] = str(payload["name"]).strip()[:32]
    if payload.get("description"):
        values["description"] = str(payload["description"]).strip()[:2000]
    if payload.get("image_key"):
        values["image_storage_key"] = str(payload["image_key"])
        values["image_content_type"] = str(payload.get("image_content_type") or "image/jpeg")
    if payload.get("remove_image"):
        values["image_storage_key"] = None
        values["image_content_type"] = None
    if values:
        values["updated_at"] = _now()
        db.execute(update(table).where(table.c.id == row["id"]).values(**values))


def _settle(db: Session, kind: str, row, proposal) -> str:
    if proposal["status"] != "open":
        return proposal["status"]
    tally = _tally(db, kind, row, proposal)
    created = proposal["created_at"]
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    outcome = None
    if tally["passing"]:
        outcome = "passed"
        _apply(db, kind, row, proposal)
    elif tally["failed"] or created < _now() - timedelta(days=PROPOSAL_DAYS):
        outcome = "rejected"
    if outcome:
        db.execute(
            update(scope_proposals)
            .where(scope_proposals.c.id == proposal["id"])
            .values(status=outcome, closed_at=_now())
        )
        return outcome
    return "open"


def _ensure_can_take_part(db: Session, kind: str, row, viewer_id: UUID) -> None:
    if viewer_id not in _memberships(db, kind, row["id"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only members can take part"
        )
    if not _is_private(kind, row):
        from app.utils.votes import ensure_established_voter

        ensure_established_voter(db, viewer_id)


def propose(
    db: Session, viewer_id: UUID, kind: str, slug: str, proposal_kind: str, payload: dict
) -> dict:
    row = _row(db, kind, slug)
    if proposal_kind not in PROPOSAL_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown proposal"
        )
    mode = _mode(kind, row)
    members = _memberships(db, kind, row["id"])
    if proposal_kind in ("remove_member", "to_organizer", "to_collaborative") and not _is_private(
        kind, row
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Only private communities can do that"
        )
    if proposal_kind == "to_collaborative":
        if mode != "organizer" or members.get(viewer_id) != "manager":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Organizers propose this"
            )
    else:
        if mode == "organizer":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Organizers make this change directly here",
            )
        _ensure_can_take_part(db, kind, row, viewer_id)
    if proposal_kind == "remove_member":
        target = payload.get("target_user_id")
        if not target or UUID(str(target)) not in members:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    if proposal_kind == "to_organizer":
        payload = {
            **payload,
            "organizer_ids": [str(value) for value in payload.get("organizer_ids") or [viewer_id]],
        }
    proposal_id = uuid4()
    db.execute(
        insert(scope_proposals).values(
            id=proposal_id,
            scope_kind=kind,
            scope_id=row["id"],
            kind=proposal_kind,
            payload={
                key: (str(value) if isinstance(value, UUID) else value)
                for key, value in payload.items()
            },
            proposer_id=viewer_id,
        )
    )
    db.execute(
        insert(scope_proposal_votes).values(proposal_id=proposal_id, voter_id=viewer_id, vote="yes")
    )
    proposal = (
        db.execute(select(scope_proposals).where(scope_proposals.c.id == proposal_id))
        .mappings()
        .one()
    )
    _settle(db, kind, row, proposal)
    db.commit()
    return settings_payload(db, viewer_id, kind, slug)


def vote(db: Session, viewer_id: UUID, proposal_id: UUID, choice: str) -> dict:
    if choice not in ("yes", "no"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="vote must be yes or no"
        )
    proposal = (
        db.execute(select(scope_proposals).where(scope_proposals.c.id == proposal_id))
        .mappings()
        .first()
    )
    if proposal is None or proposal["status"] != "open":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This vote has closed")
    kind = proposal["scope_kind"]
    table = _table(kind)
    row = db.execute(select(table).where(table.c.id == proposal["scope_id"])).mappings().one()
    if viewer_id not in _electorate(db, kind, row, proposal["kind"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can't vote on this")
    if proposal["kind"] != "to_collaborative":
        _ensure_can_take_part(db, kind, row, viewer_id)
    db.execute(
        pg_insert(scope_proposal_votes)
        .values(proposal_id=proposal_id, voter_id=viewer_id, vote=choice)
        .on_conflict_do_update(
            index_elements=[scope_proposal_votes.c.proposal_id, scope_proposal_votes.c.voter_id],
            set_={"vote": choice},
        )
    )
    _settle(db, kind, row, proposal)
    db.commit()
    return settings_payload(db, viewer_id, kind, str(row["slug"]))


# ---- direct changes (organizer mode) ------------------------------------------------------


def _ensure_organizer(db: Session, kind: str, row, viewer_id: UUID) -> None:
    if (
        _mode(kind, row) != "organizer"
        or _memberships(db, kind, row["id"]).get(viewer_id) != "manager"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only organizers can do that"
        )


def edit(db: Session, viewer_id: UUID, kind: str, slug: str, payload: dict) -> dict:
    """Organizers apply straight away; everyone else opens a member vote."""
    row = _row(db, kind, slug)
    clean = {key: value for key, value in payload.items() if value not in (None, "")}
    if not clean:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nothing to change"
        )
    if _mode(kind, row) == "organizer":
        _ensure_organizer(db, kind, row, viewer_id)
        _apply_edit(db, kind, row, clean)
        db.commit()
        return settings_payload(db, viewer_id, kind, slug)
    return propose(db, viewer_id, kind, slug, "edit", clean)


def set_organizer(
    db: Session, viewer_id: UUID, kind: str, slug: str, username: str, organizer: bool
) -> dict:
    row = _row(db, kind, slug)
    _ensure_organizer(db, kind, row, viewer_id)
    target = db.execute(select(users.c.id).where(users.c.username == username)).scalar_one_or_none()
    members = _memberships(db, kind, row["id"])
    if target is None or target not in members:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    if not organizer and sum(1 for role in members.values() if role == "manager") <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Keep at least one organizer"
        )
    db.execute(
        update(scope_memberships)
        .where(
            scope_memberships.c.scope_kind == kind,
            scope_memberships.c.scope_id == row["id"],
            scope_memberships.c.user_id == target,
        )
        .values(role="manager" if organizer else "member")
    )
    db.commit()
    return settings_payload(db, viewer_id, kind, slug)


def remove_member(db: Session, viewer_id: UUID, kind: str, slug: str, username: str) -> dict:
    row = _row(db, kind, slug)
    target = db.execute(select(users.c.id).where(users.c.username == username)).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    if _mode(kind, row) == "collaborative":
        return propose(
            db,
            viewer_id,
            kind,
            slug,
            "remove_member",
            {"target_user_id": str(target), "username": username},
        )
    _ensure_organizer(db, kind, row, viewer_id)
    if target == viewer_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Leave the community instead"
        )
    db.execute(
        delete(scope_memberships).where(
            scope_memberships.c.scope_kind == kind,
            scope_memberships.c.scope_id == row["id"],
            scope_memberships.c.user_id == target,
        )
    )
    db.commit()
    return settings_payload(db, viewer_id, kind, slug)


# ---- read ---------------------------------------------------------------------------------


def open_vote_count(db: Session, viewer_id: UUID | None, kind: str, scope_id: UUID) -> int:
    if viewer_id is None:
        return 0
    voted = select(scope_proposal_votes.c.proposal_id).where(
        scope_proposal_votes.c.voter_id == viewer_id
    )
    return int(
        db.execute(
            select(func.count())
            .select_from(scope_proposals)
            .where(
                scope_proposals.c.scope_kind == kind,
                scope_proposals.c.scope_id == scope_id,
                scope_proposals.c.status == "open",
                scope_proposals.c.id.not_in(voted),
            )
        ).scalar_one()
        or 0
    )


def settings_payload(db: Session, viewer_id: UUID | None, kind: str, slug: str) -> dict:
    row = _row(db, kind, slug)
    if kind == "community":
        assert_can_view_scope(db, viewer_id, "community", row["id"])
    members = _memberships(db, kind, row["id"])
    mode = _mode(kind, row)
    proposals = (
        db.execute(
            select(scope_proposals)
            .where(
                scope_proposals.c.scope_kind == kind,
                scope_proposals.c.scope_id == row["id"],
                scope_proposals.c.status == "open",
            )
            .order_by(scope_proposals.c.created_at)
        )
        .mappings()
        .all()
    )
    still_open = []
    for proposal in proposals:
        if _settle(db, kind, row, proposal) == "open":
            still_open.append(proposal)
    if len(still_open) != len(proposals):
        db.commit()
        row = _row(db, kind, slug)
        members = _memberships(db, kind, row["id"])
        mode = _mode(kind, row)
    names = (
        dict(
            db.execute(
                select(users.c.id, users.c.username).where(users.c.id.in_(list(members)))
            ).all()
        )
        if members
        else {}
    )
    items = []
    for proposal in still_open:
        tally = _tally(db, kind, row, proposal)
        viewer_vote = None
        if viewer_id is not None:
            viewer_vote = db.execute(
                select(scope_proposal_votes.c.vote).where(
                    scope_proposal_votes.c.proposal_id == proposal["id"],
                    scope_proposal_votes.c.voter_id == viewer_id,
                )
            ).scalar_one_or_none()
        payload = dict(proposal["payload"] or {})
        if payload.get("image_key"):
            payload["image_url"] = (
                f"/scopes/{_plural(kind)}/{slug}/image?key={payload['image_key']}"
            )
        items.append(
            {
                "id": str(proposal["id"]),
                "kind": proposal["kind"],
                "payload": payload,
                "proposer": names.get(proposal["proposer_id"]),
                "yes": tally["yes"],
                "no": tally["no"],
                "votes_required": tally["votes_required"],
                "viewer_vote": viewer_vote,
                "viewer_can_vote": bool(
                    viewer_id is not None
                    and viewer_id in _electorate(db, kind, row, proposal["kind"])
                ),
                "closes_at": (proposal["created_at"] + timedelta(days=PROPOSAL_DAYS)).isoformat(),
            }
        )
    viewer_role = members.get(viewer_id) if viewer_id is not None else None
    return {
        "scope_kind": kind,
        "slug": slug,
        "name": row["name"],
        "description": row["description"],
        "image_url": image_url(kind, row),
        "is_private": _is_private(kind, row),
        "governance": mode,
        "quorum_basis": "weekly active users" if _is_platform(kind, row) else "members",
        "member_count": len(members),
        "organizers": [
            names.get(user_id) for user_id, role in members.items() if role == "manager"
        ],
        "viewer_is_member": viewer_role is not None,
        "viewer_is_organizer": viewer_role == "manager",
        "viewer_edits_directly": mode == "organizer" and viewer_role == "manager",
        "proposals": items,
    }
