"""Atomic project close + conversion transaction helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    detail_links,
    project_conversions,
    project_inherited_decisions,
    project_links,
    project_memberships,
    project_phase_change_requests,
    project_phase_change_votes,
    project_tags,
    project_updates,
    projects,
    users,
)
from app.services.projects.helpers import PROJECT_MODES, PROJECT_SUBTYPES, _phase_for_mode
from app.services.projects.phases.labels import display_stage_label

CONVERSION_LINK_KIND = "conversion"
CONVERSION_TO_LABEL = "Converted to"
CONVERSION_FROM_LABEL = "Converted from"
INVENTORY_NOTE = (
    "Inventory, open requests, plans, signals, and roles stay on the predecessor. "
    "Only tags/scopes and memberships carry into the successor."
)
PERMANENCE_NOTE = (
    "This predecessor/successor conversion relationship is permanent and is not "
    "managed through manual project links."
)


def apply_project_close(
    db: Session,
    *,
    project_row: Any,
    close_outcome: str,
    close_note: str | None,
    author_id: UUID | None,
) -> None:
    """Mark a project closed (phase-7 + is_closed + close_outcome)."""
    outcome = (close_outcome or "close").strip().lower()
    if outcome not in {"close", "convert"}:
        outcome = "close"

    resolved_subtype = (
        str(project_row["project_subtype"]) if project_row.get("project_subtype") else None
    )
    db.execute(
        update(projects)
        .where(projects.c.id == project_row["id"])
        .values(
            current_phase_id="phase-7",
            stage_label=display_stage_label(
                str(project_row["project_mode"]),
                resolved_subtype,
                "phase-7",
            ),
            is_closed=True,
            close_outcome=outcome,
        )
    )

    note = (close_note or "").strip()
    if note:
        db.execute(
            insert(project_updates).values(
                project_id=project_row["id"],
                title="Closure note",
                body=note,
                author_id=author_id,
            )
        )


def _unique_successor_slug(db: Session, base: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in base).strip("-")
    cleaned = "-".join(part for part in cleaned.split("-") if part)[:80] or "converted"
    for _ in range(8):
        candidate = f"{cleaned}-{uuid4().hex[:8]}"
        exists = db.execute(select(projects.c.id).where(projects.c.slug == candidate)).first()
        if not exists:
            return candidate
    return f"converted-{uuid4().hex}"


def _snapshot_predecessor_history(
    db: Session,
    *,
    predecessor: Any,
    successor_id: UUID,
    electorate_size: int,
) -> None:
    phase_rows = (
        db.execute(
            select(project_phase_change_requests)
            .where(project_phase_change_requests.c.project_id == predecessor["id"])
            .order_by(project_phase_change_requests.c.created_at.asc())
        )
        .mappings()
        .all()
    )
    for req in phase_rows:
        vote_rows = db.execute(
            select(project_phase_change_votes.c.vote).where(
                project_phase_change_votes.c.request_id == req["id"]
            )
        ).all()
        yes_count = sum(1 for (vote,) in vote_rows if vote == "yes")
        no_count = sum(1 for (vote,) in vote_rows if vote == "no")
        author_username = "unknown"
        if req["author_id"] is not None:
            username = db.execute(
                select(users.c.username).where(users.c.id == req["author_id"])
            ).scalar_one_or_none()
            if username:
                author_username = username

        db.execute(
            insert(project_inherited_decisions).values(
                successor_project_id=successor_id,
                predecessor_project_id=predecessor["id"],
                predecessor_slug=predecessor["slug"],
                predecessor_title=predecessor["title"],
                source_decision_id=req["id"],
                kind="project-phase-change",
                kind_label="Inherited phase decision",
                status=req["status"],
                author_username=author_username,
                original_created_at=req["created_at"],
                electorate_size=electorate_size,
                yes_count=yes_count,
                no_count=no_count,
                approval_threshold_percent=66,
                payload={
                    "type": "phase-change",
                    "changeKind": req["change_kind"],
                    "fromPhaseId": req["from_phase_id"],
                    "toPhaseId": req["target_phase_id"],
                    "reason": req["reason"],
                    "closeOutcome": req["close_outcome"],
                    "conversionTargetMode": req["conversion_target_mode"],
                    "conversionTargetSubtype": req["conversion_target_subtype"],
                },
            )
        )


def execute_conversion(
    db: Session,
    *,
    predecessor: Any,
    request_row: Any,
    acting_user_id: UUID,
) -> dict[str, Any]:
    """Create successor project, conversion row, bidirectional links, inherited history.

    Caller owns the surrounding transaction/commit. Raises 409 on duplicate conversion.
    """
    existing = db.execute(
        select(project_conversions.c.id).where(
            project_conversions.c.predecessor_project_id == predecessor["id"]
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project has already been converted",
        )

    target_mode = (request_row.get("conversion_target_mode") or "").strip().lower()
    target_subtype_raw = request_row.get("conversion_target_subtype")
    target_subtype = (
        str(target_subtype_raw).strip().lower() if target_subtype_raw is not None else None
    )
    if target_mode not in PROJECT_MODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"conversion_target_mode must be one of: {sorted(PROJECT_MODES)}",
        )
    if target_mode == "personal-service":
        target_subtype = None
    elif target_subtype is not None and target_subtype not in PROJECT_SUBTYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"conversion_target_subtype must be one of: {sorted(PROJECT_SUBTYPES)}",
        )

    title = (request_row.get("conversion_successor_title") or "").strip()
    if not title:
        title = f"{predecessor['title']} (converted)"
    description = (request_row.get("conversion_successor_description") or "").strip()
    if not description:
        description = (request_row.get("reason") or predecessor["description"] or "").strip()

    summary = (request_row.get("reason") or "").strip() or "Converted from predecessor project."
    phase_id, stage_label = _phase_for_mode(target_mode)
    # Ensure Proposal entry even if mode helper changes later.
    phase_id = "phase-1"
    stage_label = display_stage_label(target_mode, target_subtype, phase_id)
    now = datetime.now(UTC)
    slug = _unique_successor_slug(db, title)
    author_id = request_row.get("author_id") or acting_user_id

    successor = (
        db.execute(
            insert(projects)
            .values(
                slug=slug,
                title=title[:200],
                description=description,
                author_id=author_id,
                project_mode=target_mode,
                project_subtype=target_subtype,
                current_phase_id=phase_id,
                stage_label=stage_label,
                location_label=predecessor["location_label"] or "online",
                member_count=0,
                last_activity_at=now,
                is_platform_tagged=bool(predecessor.get("is_platform_tagged")),
                is_closed=False,
                close_outcome=None,
                signal_count=0,
                vote_count=0,
                comment_count=0,
            )
            .returning(
                projects.c.id,
                projects.c.slug,
                projects.c.title,
                projects.c.description,
                projects.c.project_mode,
                projects.c.project_subtype,
            )
        )
        .mappings()
        .one()
    )

    tag_rows = (
        db.execute(select(project_tags).where(project_tags.c.project_id == predecessor["id"]))
        .mappings()
        .all()
    )
    for tag in tag_rows:
        db.execute(
            insert(project_tags).values(
                project_id=successor["id"],
                tag_kind=tag["tag_kind"],
                channel_id=tag["channel_id"],
                community_id=tag["community_id"],
            )
        )

    member_rows = (
        db.execute(
            select(project_memberships).where(project_memberships.c.project_id == predecessor["id"])
        )
        .mappings()
        .all()
    )
    for member in member_rows:
        db.execute(
            insert(project_memberships).values(
                project_id=successor["id"],
                user_id=member["user_id"],
                is_manager=False,
                is_manager_candidate=False,
                joined_at=now,
            )
        )
    db.execute(
        update(projects)
        .where(projects.c.id == successor["id"])
        .values(member_count=len(member_rows))
    )

    db.execute(
        insert(project_conversions).values(
            predecessor_project_id=predecessor["id"],
            successor_project_id=successor["id"],
            summary=summary,
            inventory_note=INVENTORY_NOTE,
            permanence_note=PERMANENCE_NOTE,
        )
    )

    db.execute(
        insert(project_links).values(
            source_project_id=predecessor["id"],
            target_project_id=successor["id"],
            relationship_label=CONVERSION_TO_LABEL,
            summary=summary,
            link_kind=CONVERSION_LINK_KIND,
            status="active",
        )
    )
    db.execute(
        insert(detail_links).values(
            source_kind="project",
            source_project_id=predecessor["id"],
            source_event_id=None,
            target_kind="project",
            target_project_id=successor["id"],
            target_event_id=None,
            relationship_label=CONVERSION_TO_LABEL,
            summary=summary,
            link_kind=CONVERSION_LINK_KIND,
            status="active",
        )
    )
    db.execute(
        insert(project_links).values(
            source_project_id=successor["id"],
            target_project_id=predecessor["id"],
            relationship_label=CONVERSION_FROM_LABEL,
            summary=summary,
            link_kind=CONVERSION_LINK_KIND,
            status="active",
        )
    )
    db.execute(
        insert(detail_links).values(
            source_kind="project",
            source_project_id=successor["id"],
            source_event_id=None,
            target_kind="project",
            target_project_id=predecessor["id"],
            target_event_id=None,
            relationship_label=CONVERSION_FROM_LABEL,
            summary=summary,
            link_kind=CONVERSION_LINK_KIND,
            status="active",
        )
    )

    _snapshot_predecessor_history(
        db,
        predecessor=predecessor,
        successor_id=successor["id"],
        electorate_size=int(predecessor.get("member_count") or len(member_rows) or 0),
    )

    return {
        "id": successor["id"],
        "slug": successor["slug"],
        "title": successor["title"],
        "project_mode": successor["project_mode"],
        "project_subtype": successor["project_subtype"],
    }


def close_and_maybe_convert(
    db: Session,
    *,
    project_row: Any,
    request_row: Any,
    acting_user_id: UUID,
) -> dict[str, Any] | None:
    """Apply close; when outcome is convert, also create successor in the same txn."""
    outcome = (request_row.get("close_outcome") or "close").strip().lower()
    if outcome not in {"close", "convert"}:
        outcome = "close"

    apply_project_close(
        db,
        project_row=project_row,
        close_outcome=outcome,
        close_note=str(request_row.get("reason") or ""),
        author_id=request_row.get("author_id") or acting_user_id,
    )

    if outcome != "convert":
        return None

    try:
        return execute_conversion(
            db,
            predecessor=project_row,
            request_row=request_row,
            acting_user_id=acting_user_id,
        )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project has already been converted",
        ) from exc
