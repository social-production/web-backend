from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    detail_link_request_votes,
    detail_link_requests,
    detail_links,
    event_memberships,
    events,
    project_memberships,
    projects,
    users,
)
from app.utils.votes import (
    required_votes,
    resolve_event_vote_population,
    resolve_project_vote_population,
)

LINK_APPROVAL_THRESHOLD = 0.66
SUBJECT_KINDS = frozenset({"project", "event"})
LINK_KIND_MANUAL = "manual"
LINK_KIND_CONVERSION = "conversion"
REQUEST_TYPE_CREATE = "create"
REQUEST_TYPE_SEVER = "sever"


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def subject_href(kind: str, slug: str) -> str:
    return f"/projects/{slug}" if kind == "project" else f"/events/{slug}"


def subject_label(kind: str) -> str:
    return "Project" if kind == "project" else "Event"


def get_subject_by_slug(db: Session, kind: str, slug: str) -> Mapping[str, object]:
    normalized_kind = (kind or "").strip().lower()
    normalized_slug = (slug or "").strip().lower()
    if normalized_kind not in SUBJECT_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"subject kind must be one of: {sorted(SUBJECT_KINDS)}",
        )

    if normalized_kind == "project":
        row = (
            db.execute(
                select(
                    projects.c.id,
                    projects.c.slug,
                    projects.c.title,
                    projects.c.description,
                    projects.c.project_mode,
                    projects.c.stage_label,
                    projects.c.location_label,
                    projects.c.member_count,
                    projects.c.is_platform_tagged,
                ).where(projects.c.slug == normalized_slug)
            )
            .mappings()
            .first()
        )
    else:
        row = (
            db.execute(
                select(
                    events.c.id,
                    events.c.slug,
                    events.c.title,
                    events.c.description,
                    events.c.current_phase_id,
                    events.c.time_label,
                    events.c.location_label,
                    events.c.scheduled_at,
                    events.c.member_count,
                ).where(events.c.slug == normalized_slug)
            )
            .mappings()
            .first()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{subject_label(normalized_kind)} not found",
        )
    return row


def get_subject_by_id(
    db: Session,
    *,
    kind: str,
    project_id: UUID | None = None,
    event_id: UUID | None = None,
) -> Mapping[str, object] | None:
    if kind == "project" and project_id is not None:
        return (
            db.execute(
                select(
                    projects.c.id,
                    projects.c.slug,
                    projects.c.title,
                    projects.c.description,
                    projects.c.project_mode,
                    projects.c.stage_label,
                    projects.c.location_label,
                    projects.c.member_count,
                    projects.c.is_platform_tagged,
                ).where(projects.c.id == project_id)
            )
            .mappings()
            .first()
        )
    if kind == "event" and event_id is not None:
        return (
            db.execute(
                select(
                    events.c.id,
                    events.c.slug,
                    events.c.title,
                    events.c.description,
                    events.c.current_phase_id,
                    events.c.time_label,
                    events.c.location_label,
                    events.c.scheduled_at,
                    events.c.member_count,
                ).where(events.c.id == event_id)
            )
            .mappings()
            .first()
        )
    return None


def _is_subject_member(db: Session, kind: str, subject_id: UUID, user_id: UUID) -> bool:
    membership_row = db.execute(
        select(
            project_memberships.c.user_id if kind == "project" else event_memberships.c.user_id
        ).where(
            (
                project_memberships.c.project_id == subject_id
                if kind == "project"
                else event_memberships.c.event_id == subject_id
            ),
            (
                project_memberships.c.user_id == user_id
                if kind == "project"
                else event_memberships.c.user_id == user_id
            ),
        )
    ).first()
    return membership_row is not None


def _ensure_subject_member(db: Session, kind: str, subject_id: UUID, user_id: UUID) -> None:
    if not _is_subject_member(db, kind, subject_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Only {subject_label(kind).lower()} members can perform this action",
        )


def _subject_vote_population(db: Session, kind: str, subject_row: Mapping[str, object]) -> int:
    if kind == "project":
        return resolve_project_vote_population(
            db, subject_row["id"], bool(subject_row.get("is_platform_tagged"))
        )
    return resolve_event_vote_population(db, subject_row["id"])


def _vote_state(
    vote_rows: Sequence[Mapping[str, object]],
    *,
    member_count: int,
    current_user_id: UUID | None,
) -> dict[str, object]:
    yes_count = 0
    no_count = 0
    active_vote = None
    for row in vote_rows:
        vote = str(row["vote"]).lower()
        if vote == "yes":
            yes_count += 1
        elif vote == "no":
            no_count += 1
        if current_user_id is not None and row["voter_id"] == current_user_id:
            active_vote = vote

    total_votes = yes_count + no_count
    approvals_required = required_votes(member_count)
    approval_ratio = (yes_count / total_votes) if total_votes > 0 else 0.0
    approval_percent = approval_ratio * 100.0
    meets_quorum = total_votes >= approvals_required
    passes = meets_quorum and approval_ratio >= LINK_APPROVAL_THRESHOLD
    remaining_eligible = max(0, member_count - total_votes)
    max_yes = yes_count + remaining_eligible
    max_total = total_votes + remaining_eligible
    can_still_pass = (
        not passes
        and max_total >= approvals_required
        and (max_yes / max_total >= LINK_APPROVAL_THRESHOLD if max_total > 0 else False)
    )
    return {
        "yesCount": yes_count,
        "noCount": no_count,
        "memberCount": member_count,
        "approvalsRequired": approvals_required,
        "approvalsRemaining": max(0, approvals_required - yes_count),
        "approvalPercent": approval_percent,
        "viewerVote": active_vote,
        "passesApprovalThreshold": passes,
        "canStillPass": can_still_pass,
        "totalVotes": total_votes,
    }


def _request_vote_states(
    db: Session,
    request_row: Mapping[str, object],
    *,
    current_user_id: UUID | None,
):
    source_subject = get_subject_by_id(
        db,
        kind=str(request_row["source_kind"]),
        project_id=request_row.get("source_project_id"),
        event_id=request_row.get("source_event_id"),
    )
    target_subject = get_subject_by_id(
        db,
        kind=str(request_row["target_kind"]),
        project_id=request_row.get("target_project_id"),
        event_id=request_row.get("target_event_id"),
    )
    if source_subject is None or target_subject is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Linked record not found")

    vote_rows = (
        db.execute(
            select(
                detail_link_request_votes.c.vote,
                detail_link_request_votes.c.voter_id,
                detail_link_request_votes.c.vote_scope,
            ).where(detail_link_request_votes.c.request_id == request_row["id"])
        )
        .mappings()
        .all()
    )
    source_votes = [row for row in vote_rows if row["vote_scope"] == "source"]
    target_votes = [row for row in vote_rows if row["vote_scope"] == "target"]
    source_state = _vote_state(
        source_votes,
        member_count=_subject_vote_population(db, str(request_row["source_kind"]), source_subject),
        current_user_id=current_user_id,
    )
    target_state = _vote_state(
        target_votes,
        member_count=_subject_vote_population(db, str(request_row["target_kind"]), target_subject),
        current_user_id=current_user_id,
    )
    return source_subject, target_subject, source_state, target_state


def _subject_detail(kind: str, subject_row: Mapping[str, object]) -> dict[str, object]:
    detail: dict[str, object] = {
        "kind": kind,
        "slug": subject_row["slug"],
        "title": subject_row["title"],
        "description": subject_row.get("description") or "",
        "href": subject_href(kind, str(subject_row["slug"])),
        "memberCount": int(subject_row.get("member_count") or 0),
        "locationLabel": subject_row.get("location_label") or "",
        "projectMode": None,
        "stageLabel": None,
        "timeLabel": None,
        "scheduledAt": None,
    }
    if kind == "project":
        detail["projectMode"] = subject_row.get("project_mode") or "productive"
        detail["stageLabel"] = subject_row.get("stage_label") or ""
    else:
        detail["stageLabel"] = (
            str(subject_row.get("current_phase_id") or "").replace("-", " ").title()
        )
        detail["timeLabel"] = subject_row.get("time_label") or ""
        detail["scheduledAt"] = _iso(subject_row.get("scheduled_at"))
    return detail


def _governance_tally(
    this_state: Mapping[str, object], other_state: Mapping[str, object]
) -> dict[str, object]:
    yes_count = int(this_state["yesCount"]) + int(other_state["yesCount"])
    no_count = int(this_state["noCount"]) + int(other_state["noCount"])
    total = yes_count + no_count
    approval_percent = ((yes_count / total) * 100.0) if total > 0 else 0.0
    if total == 0:
        label = "No votes yet"
    else:
        label = f"{int(round(approval_percent))}% · {yes_count} yes / {no_count} no"
    return {
        "yesCount": yes_count,
        "noCount": no_count,
        "approvalPercent": approval_percent,
        "label": label,
    }


def _vote_state_payload(
    state: Mapping[str, object],
    *,
    title: str,
    status_label: str,
    viewer_can_vote: bool,
    vote_scope: str,
    subject_kind: str,
    subject_slug: str,
) -> dict[str, object]:
    result_note = (
        "Approved on this side."
        if state["passesApprovalThreshold"]
        else "Waiting for more approvals."
        if state["canStillPass"]
        else "This side can no longer approve the request."
    )
    return {
        "projectTitle": title,
        "yesCount": state["yesCount"],
        "noCount": state["noCount"],
        "memberCount": state["memberCount"],
        "approvalsRequired": state["approvalsRequired"],
        "approvalsRemaining": state["approvalsRemaining"],
        "approvalPercent": state["approvalPercent"],
        "statusLabel": status_label,
        "resultNote": result_note,
        "viewerCanVote": viewer_can_vote,
        "viewerVote": state["viewerVote"],
        "voteScope": vote_scope,
        "subjectKind": subject_kind,
        "subjectSlug": subject_slug,
    }


def _request_payload(
    db: Session,
    request_row: Mapping[str, object],
    *,
    owner_kind: str,
    owner_id: UUID,
    owner_slug: str,
    owner_title: str,
    current_user_id: UUID | None,
    usernames: Mapping[UUID, str],
) -> dict[str, object]:
    source_subject, target_subject, source_state, target_state = _request_vote_states(
        db, request_row, current_user_id=current_user_id
    )
    owner_is_source = str(request_row["source_kind"]) == owner_kind and (
        (owner_kind == "project" and request_row["source_project_id"] == owner_id)
        or (owner_kind == "event" and request_row["source_event_id"] == owner_id)
    )
    counterpart_kind = str(
        request_row["target_kind"] if owner_is_source else request_row["source_kind"]
    )
    counterpart_subject = target_subject if owner_is_source else source_subject
    this_state = source_state if owner_is_source else target_state
    other_state = target_state if owner_is_source else source_state
    this_scope = "source" if owner_is_source else "target"
    other_scope = "target" if owner_is_source else "source"
    other_kind = counterpart_kind
    other_slug = str(counterpart_subject["slug"])

    viewer_can_vote_this = False
    viewer_can_vote_other = False
    if current_user_id is not None and str(request_row["status"]) == "open":
        viewer_can_vote_this = _is_subject_member(db, owner_kind, owner_id, current_user_id)
        viewer_can_vote_other = _is_subject_member(
            db, other_kind, counterpart_subject["id"], current_user_id
        )

    request_type = str(request_row.get("request_type") or REQUEST_TYPE_CREATE)
    status_label = str(request_row["status"]).replace("_", " ").title()
    if request_type == REQUEST_TYPE_SEVER and str(request_row["status"]) == "open":
        status_label = "Sever open"

    def side_vote_label(state: Mapping[str, object]) -> str:
        return f"{int(round(float(state['approvalPercent'])))}% · {state['yesCount']} yes / {state['noCount']} no"

    return {
        "id": str(request_row["id"]),
        "requestType": request_type,
        "linkId": str(request_row["link_id"]) if request_row.get("link_id") else None,
        "title": counterpart_subject["title"],
        "relationshipLabel": request_row["relationship_label"],
        "summary": request_row["summary"],
        "statusLabel": status_label,
        "proposedByUsername": usernames.get(request_row["proposed_by"], "unknown"),
        "createdAtLabel": _iso(request_row["created_at"]),
        "targetHref": subject_href(counterpart_kind, counterpart_subject["slug"]),
        "targetKind": counterpart_kind,
        "targetDetail": _subject_detail(counterpart_kind, counterpart_subject),
        "governanceTally": _governance_tally(this_state, other_state),
        "sourceTitle": source_subject["title"],
        "targetTitle": target_subject["title"],
        "sourceVoteLabel": side_vote_label(source_state),
        "targetVoteLabel": side_vote_label(target_state),
        "thisRecordVote": _vote_state_payload(
            this_state,
            title=owner_title,
            status_label=status_label,
            viewer_can_vote=viewer_can_vote_this,
            vote_scope=this_scope,
            subject_kind=owner_kind,
            subject_slug=owner_slug,
        ),
        "otherRecordVote": _vote_state_payload(
            other_state,
            title=counterpart_subject["title"],
            status_label=status_label,
            viewer_can_vote=viewer_can_vote_other,
            vote_scope=other_scope,
            subject_kind=other_kind,
            subject_slug=other_slug,
        ),
    }


def build_link_decision_history_entries(
    db: Session,
    *,
    owner_kind: str,
    owner_id: UUID,
    owner_slug: str,
    owner_title: str,
    current_user_id: UUID | None,
) -> list[tuple[object, dict[str, object]]]:
    """Return DecisionHistoryEntry-shaped link create/sever decisions for the History tab."""
    username_rows = db.execute(select(users.c.id, users.c.username)).all()
    usernames = {row[0]: row[1] for row in username_rows}

    request_rows = (
        db.execute(
            select(detail_link_requests)
            .where(
                or_(
                    and_(
                        detail_link_requests.c.source_kind == owner_kind,
                        detail_link_requests.c.source_project_id
                        == (owner_id if owner_kind == "project" else None),
                        detail_link_requests.c.source_event_id
                        == (owner_id if owner_kind == "event" else None),
                    ),
                    and_(
                        detail_link_requests.c.target_kind == owner_kind,
                        detail_link_requests.c.target_project_id
                        == (owner_id if owner_kind == "project" else None),
                        detail_link_requests.c.target_event_id
                        == (owner_id if owner_kind == "event" else None),
                    ),
                )
            )
            .order_by(detail_link_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    entries: list[tuple[object, dict[str, object]]] = []
    for request_row in request_rows:
        payload = _request_payload(
            db,
            request_row,
            owner_kind=owner_kind,
            owner_id=owner_id,
            owner_slug=owner_slug,
            owner_title=owner_title,
            current_user_id=current_user_id,
            usernames=usernames,
        )
        this_vote = payload["thisRecordVote"]
        other_vote = payload["otherRecordVote"]
        request_type = str(payload["requestType"])
        status = str(request_row["status"])
        member_count = int(this_vote["memberCount"])
        votes_required = int(this_vote["approvalsRequired"])
        total_votes = int(this_vote["yesCount"]) + int(this_vote["noCount"])
        quorum_threshold_percent = (
            (votes_required / member_count * 100.0) if member_count > 0 else 0.0
        )
        kind = (
            f"{owner_kind}-link-sever"
            if request_type == REQUEST_TYPE_SEVER
            else f"{owner_kind}-link-create"
        )
        kind_label = (
            "Sever link decision" if request_type == REQUEST_TYPE_SEVER else "Link decision"
        )
        entries.append(
            (
                request_row["created_at"],
                {
                    "id": str(request_row["id"]),
                    "entityKind": owner_kind,
                    "kind": kind,
                    "kindLabel": kind_label,
                    "createdAt": _iso(request_row["created_at"]),
                    "authorUsername": payload["proposedByUsername"],
                    "status": status,
                    "approvalThresholdPercent": int(round(LINK_APPROVAL_THRESHOLD * 100)),
                    "voteSummary": {
                        "yesCount": this_vote["yesCount"],
                        "noCount": this_vote["noCount"],
                        "totalVotes": total_votes,
                        "approvalPercent": this_vote["approvalPercent"],
                        "activeVote": this_vote["viewerVote"],
                        "meetsQuorum": total_votes >= votes_required,
                        "eligibleVoterCount": member_count,
                        "quorumThresholdPercent": quorum_threshold_percent,
                        "votesRequired": votes_required,
                        "votesRemaining": max(0, votes_required - total_votes),
                        "remainingEligibleVotes": max(0, member_count - total_votes),
                    },
                    "passesApprovalThreshold": (
                        total_votes >= votes_required
                        and float(this_vote["approvalPercent"]) >= LINK_APPROVAL_THRESHOLD * 100
                    ),
                    "canStillPass": status == "open",
                    "canVote": bool(this_vote["viewerCanVote"]),
                    "payload": {
                        "type": "link",
                        "requestType": request_type,
                        "counterpartTitle": payload["title"],
                        "counterpartKind": payload["targetKind"],
                        "counterpartHref": payload["targetHref"],
                        "relationshipLabel": payload["relationshipLabel"],
                        "summary": payload["summary"],
                        "sourceTitle": payload["sourceTitle"],
                        "targetTitle": payload["targetTitle"],
                        "sourceVoteLabel": payload["sourceVoteLabel"],
                        "targetVoteLabel": payload["targetVoteLabel"],
                        "thisSideLabel": (
                            f"{this_vote['projectTitle']}: "
                            f"{int(round(float(this_vote['approvalPercent'])))}% · "
                            f"{this_vote['yesCount']} yes / {this_vote['noCount']} no"
                        ),
                        "otherSideLabel": (
                            f"{other_vote['projectTitle']}: "
                            f"{int(round(float(other_vote['approvalPercent'])))}% · "
                            f"{other_vote['yesCount']} yes / {other_vote['noCount']} no"
                        ),
                    },
                },
            )
        )
    return entries


def create_detail_link_request(
    db: Session,
    *,
    current_user_id: UUID,
    source_kind: str,
    source_slug: str,
    target_kind: str,
    target_slug: str,
    relationship_label: str | None,
    summary: str,
) -> dict[str, object]:
    source_subject = get_subject_by_slug(db, source_kind, source_slug)
    target_subject = get_subject_by_slug(db, target_kind, target_slug)
    _ensure_subject_member(db, source_kind, source_subject["id"], current_user_id)

    if source_kind == target_kind and source_subject["id"] == target_subject["id"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Cannot link a record to itself",
        )

    cleaned_label = (relationship_label or "").strip() or "Linked"
    cleaned_summary = (summary or "").strip()
    if not cleaned_summary:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="summary is required",
        )

    duplicate = db.execute(
        select(detail_links.c.id).where(
            detail_links.c.source_kind == source_kind,
            detail_links.c.target_kind == target_kind,
            detail_links.c.source_project_id
            == (source_subject["id"] if source_kind == "project" else None),
            detail_links.c.source_event_id
            == (source_subject["id"] if source_kind == "event" else None),
            detail_links.c.target_project_id
            == (target_subject["id"] if target_kind == "project" else None),
            detail_links.c.target_event_id
            == (target_subject["id"] if target_kind == "event" else None),
            detail_links.c.status == "active",
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active link already exists between these records",
        )

    try:
        request_row = (
            db.execute(
                insert(detail_link_requests)
                .values(
                    source_kind=source_kind,
                    source_project_id=source_subject["id"] if source_kind == "project" else None,
                    source_event_id=source_subject["id"] if source_kind == "event" else None,
                    target_kind=target_kind,
                    target_project_id=target_subject["id"] if target_kind == "project" else None,
                    target_event_id=target_subject["id"] if target_kind == "event" else None,
                    relationship_label=cleaned_label,
                    summary=cleaned_summary,
                    request_type=REQUEST_TYPE_CREATE,
                    link_id=None,
                    proposed_by=current_user_id,
                    status="open",
                )
                .returning(detail_link_requests)
            )
            .mappings()
            .one()
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create link request",
        ) from exc

    return {"requestId": str(request_row["id"])}


def create_detail_link_sever_request(
    db: Session,
    *,
    current_user_id: UUID,
    subject_kind: str,
    subject_slug: str,
    link_id: UUID,
    summary: str | None = None,
) -> dict[str, object]:
    subject_row = get_subject_by_slug(db, subject_kind, subject_slug)
    _ensure_subject_member(db, subject_kind, subject_row["id"], current_user_id)

    link_row = (
        db.execute(select(detail_links).where(detail_links.c.id == link_id)).mappings().first()
    )
    if link_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link not found")
    if link_row["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active links can be severed",
        )

    owner_is_source = str(link_row["source_kind"]) == subject_kind and (
        (subject_kind == "project" and link_row["source_project_id"] == subject_row["id"])
        or (subject_kind == "event" and link_row["source_event_id"] == subject_row["id"])
    )
    owner_is_target = str(link_row["target_kind"]) == subject_kind and (
        (subject_kind == "project" and link_row["target_project_id"] == subject_row["id"])
        or (subject_kind == "event" and link_row["target_event_id"] == subject_row["id"])
    )
    if not owner_is_source and not owner_is_target:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Record is not party to this link",
        )

    existing_open = db.execute(
        select(detail_link_requests.c.id).where(
            detail_link_requests.c.link_id == link_id,
            detail_link_requests.c.request_type == REQUEST_TYPE_SEVER,
            detail_link_requests.c.status == "open",
        )
    ).first()
    if existing_open is not None:
        return {"requestId": str(existing_open[0])}

    cleaned_summary = (summary or "").strip() or "Propose severing this link."
    try:
        request_row = (
            db.execute(
                insert(detail_link_requests)
                .values(
                    source_kind=link_row["source_kind"],
                    source_project_id=link_row["source_project_id"],
                    source_event_id=link_row["source_event_id"],
                    target_kind=link_row["target_kind"],
                    target_project_id=link_row["target_project_id"],
                    target_event_id=link_row["target_event_id"],
                    relationship_label=link_row["relationship_label"],
                    summary=cleaned_summary,
                    request_type=REQUEST_TYPE_SEVER,
                    link_id=link_id,
                    proposed_by=current_user_id,
                    status="open",
                )
                .returning(detail_link_requests)
            )
            .mappings()
            .one()
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create sever request",
        ) from exc

    return {"requestId": str(request_row["id"])}


def vote_detail_link_request(
    db: Session,
    *,
    current_user_id: UUID,
    subject_kind: str,
    subject_slug: str,
    request_id: UUID,
    vote: str,
) -> dict[str, object]:
    subject_row = get_subject_by_slug(db, subject_kind, subject_slug)
    _ensure_subject_member(db, subject_kind, subject_row["id"], current_user_id)

    request_row = (
        db.execute(select(detail_link_requests).where(detail_link_requests.c.id == request_id))
        .mappings()
        .first()
    )
    if request_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Link request not found")
    if request_row["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Link request is already closed",
        )

    normalized_vote = (vote or "").strip().lower()
    if normalized_vote not in {"yes", "no"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="vote must be one of: ['no', 'yes']",
        )

    if str(request_row["source_kind"]) == subject_kind and (
        (subject_kind == "project" and request_row["source_project_id"] == subject_row["id"])
        or (subject_kind == "event" and request_row["source_event_id"] == subject_row["id"])
    ):
        vote_scope = "source"
    elif str(request_row["target_kind"]) == subject_kind and (
        (subject_kind == "project" and request_row["target_project_id"] == subject_row["id"])
        or (subject_kind == "event" and request_row["target_event_id"] == subject_row["id"])
    ):
        vote_scope = "target"
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Record is not party to this link request",
        )

    existing_vote = db.execute(
        select(detail_link_request_votes.c.vote).where(
            detail_link_request_votes.c.request_id == request_id,
            detail_link_request_votes.c.voter_id == current_user_id,
            detail_link_request_votes.c.vote_scope == vote_scope,
        )
    ).first()

    try:
        if existing_vote is None:
            db.execute(
                insert(detail_link_request_votes).values(
                    request_id=request_id,
                    voter_id=current_user_id,
                    vote=normalized_vote,
                    vote_scope=vote_scope,
                )
            )
        else:
            db.execute(
                update(detail_link_request_votes)
                .where(
                    detail_link_request_votes.c.request_id == request_id,
                    detail_link_request_votes.c.voter_id == current_user_id,
                    detail_link_request_votes.c.vote_scope == vote_scope,
                )
                .values(vote=normalized_vote)
            )

        request_row = (
            db.execute(select(detail_link_requests).where(detail_link_requests.c.id == request_id))
            .mappings()
            .one()
        )
        _source_subject, _target_subject, source_state, target_state = _request_vote_states(
            db, request_row, current_user_id=current_user_id
        )
        request_type = str(request_row.get("request_type") or REQUEST_TYPE_CREATE)

        if source_state["passesApprovalThreshold"] and target_state["passesApprovalThreshold"]:
            db.execute(
                update(detail_link_requests)
                .where(detail_link_requests.c.id == request_id)
                .values(status="approved")
            )
            if request_type == REQUEST_TYPE_SEVER and request_row.get("link_id") is not None:
                db.execute(
                    update(detail_links)
                    .where(detail_links.c.id == request_row["link_id"])
                    .values(status="inactive")
                )
            elif request_type == REQUEST_TYPE_CREATE:
                db.execute(
                    insert(detail_links).values(
                        source_kind=request_row["source_kind"],
                        source_project_id=request_row["source_project_id"],
                        source_event_id=request_row["source_event_id"],
                        target_kind=request_row["target_kind"],
                        target_project_id=request_row["target_project_id"],
                        target_event_id=request_row["target_event_id"],
                        relationship_label=request_row["relationship_label"],
                        summary=request_row["summary"],
                        link_kind=LINK_KIND_MANUAL,
                        status="active",
                    )
                )
        elif (not source_state["passesApprovalThreshold"] and not source_state["canStillPass"]) or (
            not target_state["passesApprovalThreshold"] and not target_state["canStillPass"]
        ):
            db.execute(
                update(detail_link_requests)
                .where(detail_link_requests.c.id == request_id)
                .values(status="rejected")
            )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not record link vote",
        ) from exc

    return {"ok": True}


def build_links_frame(
    db: Session,
    *,
    owner_kind: str,
    owner_slug: str,
    current_user_id: UUID | None,
) -> dict[str, object]:
    owner = get_subject_by_slug(db, owner_kind, owner_slug)
    owner_id = owner["id"]
    viewer_can_propose_links = False
    if current_user_id is not None:
        viewer_can_propose_links = _is_subject_member(db, owner_kind, owner_id, current_user_id)

    username_rows = db.execute(select(users.c.id, users.c.username)).all()
    usernames = {row[0]: row[1] for row in username_rows}

    open_sever_by_link: dict[str, Mapping[str, object]] = {}
    sever_rows = (
        db.execute(
            select(detail_link_requests).where(
                detail_link_requests.c.request_type == REQUEST_TYPE_SEVER,
                detail_link_requests.c.status == "open",
                detail_link_requests.c.link_id.is_not(None),
            )
        )
        .mappings()
        .all()
    )
    for sever_row in sever_rows:
        open_sever_by_link[str(sever_row["link_id"])] = sever_row

    link_rows = (
        db.execute(
            select(detail_links).where(
                or_(
                    and_(
                        detail_links.c.source_kind == owner_kind,
                        detail_links.c.source_project_id
                        == (owner_id if owner_kind == "project" else None),
                        detail_links.c.source_event_id
                        == (owner_id if owner_kind == "event" else None),
                    ),
                    and_(
                        detail_links.c.target_kind == owner_kind,
                        detail_links.c.target_project_id
                        == (owner_id if owner_kind == "project" else None),
                        detail_links.c.target_event_id
                        == (owner_id if owner_kind == "event" else None),
                    ),
                )
            )
        )
        .mappings()
        .all()
    )
    active_links: list[dict[str, object]] = []
    historical_links: list[dict[str, object]] = []
    for row in link_rows:
        owner_is_source = str(row["source_kind"]) == owner_kind and (
            (owner_kind == "project" and row["source_project_id"] == owner_id)
            or (owner_kind == "event" and row["source_event_id"] == owner_id)
        )
        counterpart_kind = str(row["target_kind"] if owner_is_source else row["source_kind"])
        counterpart = get_subject_by_id(
            db,
            kind=counterpart_kind,
            project_id=row["target_project_id"] if owner_is_source else row["source_project_id"],
            event_id=row["target_event_id"] if owner_is_source else row["source_event_id"],
        )
        if counterpart is None:
            continue

        item: dict[str, object] = {
            "id": str(row["id"]),
            "title": counterpart["title"],
            "relationshipLabel": row["relationship_label"],
            "summary": row["summary"],
            "href": subject_href(counterpart_kind, counterpart["slug"]),
            "subjectKind": counterpart_kind,
            "subjectLabel": subject_label(counterpart_kind),
            "linkKind": row["link_kind"],
            "targetDetail": _subject_detail(counterpart_kind, counterpart),
            "governanceTally": {
                "yesCount": 0,
                "noCount": 0,
                "approvalPercent": 100.0 if row["status"] == "active" else 0.0,
                "label": "Linked" if row["status"] == "active" else "Inactive",
            },
            "openSeverRequest": None,
            "viewerCanProposeSever": viewer_can_propose_links and row["status"] == "active",
        }

        open_sever = open_sever_by_link.get(str(row["id"]))
        if open_sever is not None:
            sever_payload = _request_payload(
                db,
                open_sever,
                owner_kind=owner_kind,
                owner_id=owner_id,
                owner_slug=owner_slug,
                owner_title=str(owner["title"]),
                current_user_id=current_user_id,
                usernames=usernames,
            )
            item["openSeverRequest"] = sever_payload
            item["governanceTally"] = sever_payload["governanceTally"]

        if row["status"] == "active":
            active_links.append(item)
        else:
            historical_links.append(item)

    request_rows = (
        db.execute(
            select(detail_link_requests)
            .where(
                or_(
                    and_(
                        detail_link_requests.c.source_kind == owner_kind,
                        detail_link_requests.c.source_project_id
                        == (owner_id if owner_kind == "project" else None),
                        detail_link_requests.c.source_event_id
                        == (owner_id if owner_kind == "event" else None),
                    ),
                    and_(
                        detail_link_requests.c.target_kind == owner_kind,
                        detail_link_requests.c.target_project_id
                        == (owner_id if owner_kind == "project" else None),
                        detail_link_requests.c.target_event_id
                        == (owner_id if owner_kind == "event" else None),
                    ),
                )
            )
            .order_by(detail_link_requests.c.created_at.desc())
        )
        .mappings()
        .all()
    )

    pending_requests: list[dict[str, object]] = []
    historical_requests: list[dict[str, object]] = []
    for request_row in request_rows:
        # Open sever requests are rendered on the active link card.
        if (
            str(request_row.get("request_type") or REQUEST_TYPE_CREATE) == REQUEST_TYPE_SEVER
            and str(request_row["status"]) == "open"
        ):
            continue
        payload = _request_payload(
            db,
            request_row,
            owner_kind=owner_kind,
            owner_id=owner_id,
            owner_slug=owner_slug,
            owner_title=str(owner["title"]),
            current_user_id=current_user_id,
            usernames=usernames,
        )
        if request_row["status"] == "open":
            pending_requests.append(payload)
        else:
            request_type = str(request_row.get("request_type") or REQUEST_TYPE_CREATE)
            status = str(request_row["status"])
            if request_type == REQUEST_TYPE_CREATE and status == "approved":
                continue
            historical_requests.append(payload)

    project_candidates = db.execute(
        select(projects.c.slug, projects.c.title)
        .where(projects.c.slug != owner_slug if owner_kind == "project" else True)
        .order_by(projects.c.title.asc())
        .limit(20)
    ).all()
    event_candidates = db.execute(
        select(events.c.slug, events.c.title)
        .where(events.c.slug != owner_slug if owner_kind == "event" else True)
        .order_by(events.c.title.asc())
        .limit(20)
    ).all()
    linkable_records = [
        {
            "kind": "project",
            "slug": slug,
            "title": title,
            "href": subject_href("project", slug),
            "label": f"Project · {title}",
        }
        for slug, title in project_candidates
    ] + [
        {
            "kind": "event",
            "slug": slug,
            "title": title,
            "href": subject_href("event", slug),
            "label": f"Event · {title}",
        }
        for slug, title in event_candidates
    ]

    intro = (
        "Use links to connect this project with related projects and events."
        if owner_kind == "project"
        else "Use links to connect this event with related projects and events."
    )
    return {
        "ownerKind": owner_kind,
        "ownerSlug": owner_slug,
        "intro": intro,
        "activeLinks": active_links,
        "pendingLinkRequests": pending_requests,
        "historicalLinks": historical_links,
        "historicalLinkRequests": historical_requests,
        "linkableRecords": linkable_records,
        "viewerCanProposeLinks": viewer_can_propose_links,
        "conversionNote": "",
        "conversionWorkflow": [],
        "conversionLineage": None,
    }
