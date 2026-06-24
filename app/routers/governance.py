from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.dependencies import bearer_scheme, get_current_user_id, get_current_user_token_payload
from app.dependencies import get_db
from app.services.governance import add_comment, cast_vote, get_comments
from app.services.governance import submit_report, vote_report

router = APIRouter(prefix="/governance", tags=["governance"])


async def _get_optional_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> UUID | None:
    if credentials is None or not credentials.credentials:
        return None

    try:
        payload = await get_current_user_token_payload(credentials)
    except HTTPException:
        return None

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        return None

    try:
        return UUID(subject)
    except ValueError:
        return None


class CommentCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subject_type: str = Field(pattern="^(thread|post|event|project)$")
    subject_id: UUID
    body: str = Field(min_length=1)
    parent_id: UUID | None = None


class VoteCastRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_type: str = Field(pattern="^(thread|post|comment|event|project)$")
    target_id: UUID
    direction: str = Field(pattern="^(up|down|neutral)$")


class CommentOut(BaseModel):
    id: UUID
    subject_type: str
    subject_id: UUID
    parent_id: UUID | None = None
    author_id: UUID | None = None
    author_username: str = ""
    body: str
    vote_count: int
    active_vote: int = 0
    created_at: object
    updated_at: object
    replies: list["CommentOut"] = Field(default_factory=list)


class CommentCreateResponse(BaseModel):
    comment: CommentOut


class CommentsListResponse(BaseModel):
    subject_type: str
    subject_id: UUID
    total: int
    items: list[CommentOut]


class VoteCastResponse(BaseModel):
    target_type: str
    target_id: UUID
    direction: str
    value: int


class ReportSubmitRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_type: str = Field(pattern="^(project|thread|post|comment)$")
    target_id: UUID
    reason: str = Field(pattern="^(spam|serious-harm)$")
    description: str = Field(min_length=1)


class ReportVoteRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    vote: str = Field(pattern="^(yes|no)$")


class ReportVoteSummaryOut(BaseModel):
    yes_count: int
    no_count: int
    active_vote: str | None = None
    eligible_voter_count: int
    votes_required: int


class ReportOut(BaseModel):
    id: UUID
    subject_type: str
    subject_id: UUID
    target_type: str
    target_id: UUID
    reason: str
    description: str
    reporter_id: UUID | None = None
    reported_author_id: UUID | None = None
    resolution: str
    created_at: object
    updated_at: object
    vote_summary: ReportVoteSummaryOut


class ReportSubmitResponse(BaseModel):
    report: ReportOut


class ReportVoteResponse(BaseModel):
    report: ReportOut
    vote: str


@router.post("/comments", response_model=CommentCreateResponse)
def create_comment(
    payload: CommentCreateRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return add_comment(
        db=db,
        current_user_id=current_user_id,
        subject_type=payload.subject_type,
        subject_id=payload.subject_id,
        body=payload.body,
        parent_id=payload.parent_id,
    )


@router.get("/comments", response_model=CommentsListResponse)
def list_comments(
    subject_type: str = Query(pattern="^(thread|post|event|project)$"),
    subject_id: UUID = Query(),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(_get_optional_user_id),
) -> dict[str, object]:
    return get_comments(db=db, subject_type=subject_type, subject_id=subject_id, current_user_id=current_user_id)


@router.post("/votes", response_model=VoteCastResponse)
def create_vote(
    payload: VoteCastRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return cast_vote(
        db=db,
        current_user_id=current_user_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        direction=payload.direction,
    )


@router.post("/reports", response_model=ReportSubmitResponse)
def create_report(
    payload: ReportSubmitRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return submit_report(
        db=db,
        current_user_id=current_user_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        reason=payload.reason,
        description=payload.description,
    )


@router.post("/reports/{report_id}/vote", response_model=ReportVoteResponse)
def cast_report_vote(
    report_id: UUID,
    payload: ReportVoteRequest,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return vote_report(
        db=db,
        current_user_id=current_user_id,
        report_id=report_id,
        vote=payload.vote,
    )
