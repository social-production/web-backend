from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session
from starlette.formparsers import MultiPartException

from app.auth.dependencies import get_current_user_id, get_optional_current_user_id
from app.dependencies import get_db
from app.services.governance import add_comment, cast_vote, get_comments, submit_report, vote_report
from app.services.messages.attachments import read_comment_attachment

MAX_UPLOAD_PART_BYTES = 12 * 1024 * 1024

router = APIRouter(prefix="/governance", tags=["governance"])


class CommentCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subject_type: str = Field(pattern="^(thread|post|event|project|help_request)$")
    subject_id: UUID
    body: str = Field(min_length=1)
    parent_id: UUID | None = None


class VoteCastRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    target_type: str = Field(
        pattern="^(thread|post|comment|event|project|help_request|platform_feedback)$"
    )
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
    moderation_state: str = "visible"
    moderation_reason: str | None = None
    report: dict[str, object] | None = None
    replies: list[CommentOut] = Field(default_factory=list)
    attachments: list[dict[str, object]] = Field(default_factory=list)


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

    target_type: str = Field(pattern="^(project|thread|post|comment|event|help_request|message)$")
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
    audience_size: int = 0
    total_votes: int = 0
    votes_required: int
    required_yes_share: float = 0.66
    delete_yes_share: float = 0.66
    hide_yes_share: float = 0.66
    delete_quorum: int = 1
    hide_quorum: int = 1
    removal_quorum: int = 1
    restriction_quorum: int = 1
    restriction_votes_required: int = 1
    confirming_votes_required: int | None = None


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


def _content_disposition(kind: str, filename: str) -> str:
    safe = filename.replace("\\", "_").replace('"', "").replace("\r", "").replace("\n", "")
    disposition = "inline" if kind == "image" else "attachment"
    return f'{disposition}; filename="{safe}"'


@router.post("/comments", response_model=CommentCreateResponse)
async def create_comment(
    request: Request,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    attachments: list = []
    if "multipart/form-data" in content_type:
        try:
            form = await request.form(max_part_size=MAX_UPLOAD_PART_BYTES)
        except MultiPartException as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Files must be 10MB or smaller",
            ) from exc
        raw_body = form.get("body")
        body = raw_body if isinstance(raw_body, str) else ""
        subject_type = form.get("subject_type")
        subject_id = form.get("subject_id")
        parent_raw = form.get("parent_id")
        if not isinstance(subject_type, str) or not isinstance(subject_id, str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="subject_type and subject_id are required",
            )
        try:
            parsed_subject_id = UUID(subject_id)
            parsed_parent_id = (
                UUID(parent_raw) if isinstance(parent_raw, str) and parent_raw else None
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="subject_id must be a UUID",
            ) from exc
        from app.services.messages.attachments import prepare_form_uploads

        attachments = await prepare_form_uploads(form)
        return add_comment(
            db=db,
            current_user_id=current_user_id,
            subject_type=subject_type,
            subject_id=parsed_subject_id,
            body=body,
            parent_id=parsed_parent_id,
            attachments=attachments,
        )

    try:
        payload = CommentCreateRequest.model_validate(await request.json())
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Message body is required",
        ) from exc
    return add_comment(
        db=db,
        current_user_id=current_user_id,
        subject_type=payload.subject_type,
        subject_id=payload.subject_id,
        body=payload.body,
        parent_id=payload.parent_id,
    )


@router.get("/attachments/{attachment_id}")
def download_comment_attachment(
    attachment_id: UUID,
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> Response:
    payload = read_comment_attachment(
        db=db, current_user_id=current_user_id, attachment_id=attachment_id
    )
    return Response(
        content=payload["data"],
        media_type=str(payload["content_type"]),
        headers={
            "Content-Disposition": _content_disposition(
                str(payload["kind"]), str(payload["filename"])
            ),
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/comments", response_model=CommentsListResponse)
def list_comments(
    subject_type: str = Query(pattern="^(thread|post|event|project|help_request)$"),
    subject_id: UUID = Query(),
    db: Session = Depends(get_db),
    current_user_id: UUID | None = Depends(get_optional_current_user_id),
) -> dict[str, object]:
    return get_comments(
        db=db, subject_type=subject_type, subject_id=subject_id, current_user_id=current_user_id
    )


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
