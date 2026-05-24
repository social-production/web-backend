from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.projects_software import (
    DECISION_KIND_MERGE_CAPABILITY,
    DECISION_KIND_PULL_REQUEST,
    DECISION_KIND_REPOSITORY_REPLACEMENT,
    list_software_requests,
    record_pull_request_merge,
    request_merge_capability_change,
    request_repository_replacement,
    submit_pull_request,
    vote_software_request,
)

router = APIRouter(prefix="/projects", tags=["projects-software"])


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


class SoftwareDecisionOut(BaseModel):
    id: UUID
    decision_kind: str
    status: str
    author_id: UUID | None = None
    created_at: object
    resolved_at: object | None = None
    payload: dict[str, object]
    vote_summary: VoteSummaryOut


class SoftwareDecisionResponse(BaseModel):
    request: SoftwareDecisionOut


class SoftwareDecisionListResponse(BaseModel):
    project_slug: str
    total: int
    items: list[SoftwareDecisionOut]


class SoftwareVoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


class SoftwareVoteResponse(BaseModel):
    request: SoftwareDecisionOut
    vote: str
    executed: bool


class PullRequestSubmitIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1)
    pull_request_url: str = Field(min_length=1)


class MergeCapabilityRequestIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_user_id: UUID
    enable_merge: bool = True
    reason: str = Field(min_length=1)


class RepositoryReplacementRequestIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    new_repository_url: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class PullRequestMergeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    merge_commit_id: str = Field(min_length=1)


class PullRequestMergeResponse(BaseModel):
    request: SoftwareDecisionOut
    merged: bool
    merge_commit_id: str


@router.post("/{slug}/software/pull-requests", response_model=SoftwareDecisionResponse)
def submit_project_pull_request(
    slug: str,
    payload: PullRequestSubmitIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return submit_pull_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        title=payload.title,
        description=payload.description,
        pull_request_url=payload.pull_request_url,
    )


@router.get("/{slug}/software/pull-requests", response_model=SoftwareDecisionListResponse)
def list_project_pull_requests(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_software_requests(db=db, project_slug=slug, kind=DECISION_KIND_PULL_REQUEST)


@router.post("/{slug}/software/pull-requests/{decision_id}/vote", response_model=SoftwareVoteResponse)
def vote_project_pull_request(
    slug: str,
    decision_id: UUID,
    payload: SoftwareVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_software_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        decision_id=decision_id,
        kind=DECISION_KIND_PULL_REQUEST,
        vote=payload.vote,
    )


@router.post("/{slug}/software/pull-requests/{decision_id}/merge", response_model=PullRequestMergeResponse)
def merge_project_pull_request(
    slug: str,
    decision_id: UUID,
    payload: PullRequestMergeIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return record_pull_request_merge(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        decision_id=decision_id,
        merge_commit_id=payload.merge_commit_id,
    )


@router.post("/{slug}/software/merge-capability-requests", response_model=SoftwareDecisionResponse)
def submit_merge_capability_request(
    slug: str,
    payload: MergeCapabilityRequestIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return request_merge_capability_change(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        target_user_id=payload.target_user_id,
        enable_merge=payload.enable_merge,
        reason=payload.reason,
    )


@router.get("/{slug}/software/merge-capability-requests", response_model=SoftwareDecisionListResponse)
def list_merge_capability_requests(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_software_requests(db=db, project_slug=slug, kind=DECISION_KIND_MERGE_CAPABILITY)


@router.post("/{slug}/software/merge-capability-requests/{decision_id}/vote", response_model=SoftwareVoteResponse)
def vote_merge_capability_request(
    slug: str,
    decision_id: UUID,
    payload: SoftwareVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_software_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        decision_id=decision_id,
        kind=DECISION_KIND_MERGE_CAPABILITY,
        vote=payload.vote,
    )


@router.post("/{slug}/software/repository-replacement-requests", response_model=SoftwareDecisionResponse)
def submit_repository_replacement_request(
    slug: str,
    payload: RepositoryReplacementRequestIn,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return request_repository_replacement(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        new_repository_url=payload.new_repository_url,
        reason=payload.reason,
    )


@router.get("/{slug}/software/repository-replacement-requests", response_model=SoftwareDecisionListResponse)
def list_repository_replacement_requests(
    slug: str,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return list_software_requests(db=db, project_slug=slug, kind=DECISION_KIND_REPOSITORY_REPLACEMENT)


@router.post("/{slug}/software/repository-replacement-requests/{decision_id}/vote", response_model=SoftwareVoteResponse)
def vote_repository_replacement_request(
    slug: str,
    decision_id: UUID,
    payload: SoftwareVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_software_request(
        db=db,
        current_user_id=current_user_id,
        project_slug=slug,
        decision_id=decision_id,
        kind=DECISION_KIND_REPOSITORY_REPLACEMENT,
        vote=payload.vote,
    )
