from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.events_phases import (
    create_edit_request,
    create_phase_change_request,
    create_update_request,
    list_edit_requests,
    list_phase_change_requests,
    list_update_requests,
    vote_edit_request,
    vote_phase_change_request,
    vote_update_request,
)

router = APIRouter(prefix="/events", tags=["events-phases"])


class VoteSummaryOut(BaseModel):
    yes_count: int
    no_count: int
    total_votes: int
    approval_ratio: float
    approval_threshold: float
    votes_required: int
    member_count: int
    meets_quorum: bool
    meets_approval: bool
    is_passing: bool


class PhaseChangeRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_phase_id: str = Field(min_length=1, max_length=24)
    reason: str = Field(min_length=1)


class PhaseChangeVoteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


class EventPhaseChangeRequestOut(BaseModel):
    id: UUID
    event_id: UUID
    from_phase_id: str
    target_phase_id: str
    change_kind: str
    reason: str
    author_id: UUID | None = None
    status: str
    created_at: object
    vote_summary: VoteSummaryOut


class EventPhaseChangeRequestResponse(BaseModel):
    request: EventPhaseChangeRequestOut


class EventPhaseChangeRequestListResponse(BaseModel):
    event_slug: str
    current_phase_id: str
    total: int
    items: list[EventPhaseChangeRequestOut]


class EventPhaseChangeVoteResponse(BaseModel):
    request: EventPhaseChangeRequestOut
    vote: str
    executed: bool
    current_phase_id: str


class EventUpdateRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1)


class EventUpdateRequestOut(BaseModel):
    id: UUID
    event_id: UUID
    body: str
    author_id: UUID | None = None
    status: str
    created_at: object
    vote_summary: VoteSummaryOut


class EventUpdateRequestResponse(BaseModel):
    request: EventUpdateRequestOut


class EventUpdateRequestListResponse(BaseModel):
    event_slug: str
    total: int
    items: list[EventUpdateRequestOut]


class EventUpdateVoteResponse(BaseModel):
    request: EventUpdateRequestOut
    vote: str
    executed: bool


class EventEditRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)


class EventEditRequestOut(BaseModel):
    id: UUID
    event_id: UUID
    title: str
    description: str
    author_id: UUID | None = None
    status: str
    created_at: object
    vote_summary: VoteSummaryOut


class EventEditRequestResponse(BaseModel):
    request: EventEditRequestOut


class EventEditRequestListResponse(BaseModel):
    event_slug: str
    total: int
    items: list[EventEditRequestOut]


class EventEditVoteResponse(BaseModel):
    request: EventEditRequestOut
    vote: str
    executed: bool


@router.post("/{slug}/phase-requests", response_model=EventPhaseChangeRequestResponse)
def create_event_phase_request(
    slug: str,
    payload: PhaseChangeRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_phase_change_request(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        target_phase_id=payload.target_phase_id,
        reason=payload.reason,
    )


@router.get("/{slug}/phase-requests", response_model=EventPhaseChangeRequestListResponse)
def get_event_phase_requests(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_phase_change_requests(db=db, event_slug=slug)


@router.post("/{slug}/phase-requests/{request_id}/vote", response_model=EventPhaseChangeVoteResponse)
def vote_event_phase_request(
    slug: str,
    request_id: UUID,
    payload: PhaseChangeVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_phase_change_request(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/update-requests", response_model=EventUpdateRequestResponse)
def create_event_update_request(
    slug: str,
    payload: EventUpdateRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_update_request(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        body=payload.body,
    )


@router.get("/{slug}/update-requests", response_model=EventUpdateRequestListResponse)
def get_event_update_requests(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_update_requests(db=db, event_slug=slug)


@router.post("/{slug}/update-requests/{request_id}/vote", response_model=EventUpdateVoteResponse)
def vote_event_update_request(
    slug: str,
    request_id: UUID,
    payload: PhaseChangeVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_update_request(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/edit-requests", response_model=EventEditRequestResponse)
def create_event_edit_request(
    slug: str,
    payload: EventEditRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_edit_request(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        title=payload.title,
        description=payload.description,
    )


@router.get("/{slug}/edit-requests", response_model=EventEditRequestListResponse)
def get_event_edit_requests(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_edit_requests(db=db, event_slug=slug)


@router.post("/{slug}/edit-requests/{request_id}/vote", response_model=EventEditVoteResponse)
def vote_event_edit_request(
    slug: str,
    request_id: UUID,
    payload: PhaseChangeVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_edit_request(
        db=db,
        current_user_id=current_user_id,
        event_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )
