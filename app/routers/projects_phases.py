from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.projects_phases import (
    advance_project_phase,
    create_project_edit_request,
    create_project_update_request,
    create_revert_phase_change_request,
    create_phase_change_request,
    list_phase_change_requests,
    vote_project_edit_request,
    vote_project_update_request,
    vote_revert_phase_change_request,
    vote_phase_change_request,
)

router = APIRouter(prefix="/projects", tags=["projects-phases"])


class PhaseChangeRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_phase_id: str = Field(min_length=1, max_length=24)
    reason: str = Field(min_length=1)


class PhaseChangeVoteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


class PhaseChangeVoteSummaryOut(BaseModel):
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


class PhaseChangeRequestOut(BaseModel):
    id: UUID
    project_id: UUID
    from_phase_id: str
    target_phase_id: str
    change_kind: str
    close_outcome: str | None = None
    conversion_target_mode: str | None = None
    conversion_target_subtype: str | None = None
    reason: str
    author_id: UUID | None = None
    status: str
    created_at: object
    vote_summary: PhaseChangeVoteSummaryOut


class PhaseChangeRequestResponse(BaseModel):
    request: PhaseChangeRequestOut


class PhaseChangeRequestListResponse(BaseModel):
    project_slug: str
    project_mode: str
    current_phase_id: str
    total: int
    items: list[PhaseChangeRequestOut]


class PhaseChangeVoteResponse(BaseModel):
    request: PhaseChangeRequestOut
    vote: str
    executed: bool
    current_phase_id: str


class ProjectUpdateRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1)


class ProjectEditRequestCreateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)


class ProjectUpdateRequestOut(BaseModel):
    id: UUID
    project_id: UUID
    body: str
    author_id: UUID | None = None
    status: str
    created_at: object
    vote_summary: PhaseChangeVoteSummaryOut


class ProjectEditRequestOut(BaseModel):
    id: UUID
    project_id: UUID
    title: str
    description: str
    author_id: UUID | None = None
    status: str
    created_at: object
    vote_summary: PhaseChangeVoteSummaryOut


class ProjectUpdateRequestResponse(BaseModel):
    request: ProjectUpdateRequestOut


class ProjectEditRequestResponse(BaseModel):
    request: ProjectEditRequestOut


class ProjectUpdateVoteResponse(BaseModel):
    request: ProjectUpdateRequestOut
    vote: str
    executed: bool


class ProjectEditVoteResponse(BaseModel):
    request: ProjectEditRequestOut
    vote: str
    executed: bool


class ProjectPhaseAdvanceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    close_note: str | None = None


@router.post("/{slug}/phase-requests", response_model=PhaseChangeRequestResponse)
def create_project_phase_request(
    slug: str,
    payload: PhaseChangeRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_phase_change_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        target_phase_id=payload.target_phase_id,
        reason=payload.reason,
    )


@router.get("/{slug}/phase-requests", response_model=PhaseChangeRequestListResponse)
def get_project_phase_requests(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_phase_change_requests(db=db, project_slug=slug)


@router.post("/{slug}/phase-requests/{request_id}/vote", response_model=PhaseChangeVoteResponse)
def vote_project_phase_request(
    slug: str,
    request_id: UUID,
    payload: PhaseChangeVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_phase_change_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/update-requests", response_model=ProjectUpdateRequestResponse)
def create_project_update_request_route(
    slug: str,
    payload: ProjectUpdateRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_project_update_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        body=payload.body,
    )


@router.post("/{slug}/update-requests/{request_id}/vote", response_model=ProjectUpdateVoteResponse)
def vote_project_update_request_route(
    slug: str,
    request_id: UUID,
    payload: PhaseChangeVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_project_update_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/edit-requests", response_model=ProjectEditRequestResponse)
def create_project_edit_request_route(
    slug: str,
    payload: ProjectEditRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_project_edit_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        title=payload.title,
        description=payload.description,
    )


@router.post("/{slug}/edit-requests/{request_id}/vote", response_model=ProjectEditVoteResponse)
def vote_project_edit_request_route(
    slug: str,
    request_id: UUID,
    payload: PhaseChangeVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_project_edit_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/revert-requests", response_model=PhaseChangeRequestResponse)
def create_project_revert_request_route(
    slug: str,
    payload: PhaseChangeRequestCreateIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return create_revert_phase_change_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        target_phase_id=payload.target_phase_id,
        reason=payload.reason,
    )


@router.post("/{slug}/revert-requests/{request_id}/vote", response_model=PhaseChangeVoteResponse)
def vote_project_revert_request_route(
    slug: str,
    request_id: UUID,
    payload: PhaseChangeVoteIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_revert_phase_change_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        request_id=request_id,
        vote=payload.vote,
    )


@router.post("/{slug}/phase-advance")
def advance_project_phase_route(
    slug: str,
    payload: ProjectPhaseAdvanceIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return advance_project_phase(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        close_note=payload.close_note,
    )
