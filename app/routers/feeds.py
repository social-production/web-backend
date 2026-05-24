from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.feeds import get_home_feed, get_public_feed

router = APIRouter(prefix="/feeds", tags=["feeds"])


class FeedItemOut(BaseModel):
    id: UUID
    entity_type: str
    slug: str
    title: str
    body: str
    author_id: UUID | None = None
    signal_count: int
    vote_count: int
    comment_count: int
    member_count: int
    going_count: int
    last_activity_at: object
    created_at: object


class FeedResponse(BaseModel):
    total: int
    sort: str
    limit: int
    offset: int
    items: list[FeedItemOut]


@router.get("/public", response_model=FeedResponse)
def public_feed(
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_public_feed(db=db, sort=sort, limit=limit, offset=offset)


@router.get("/home", response_model=FeedResponse)
def home_feed(
    sort: str = Query(default="recent", pattern="^(popular|recent)$"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return get_home_feed(
        db=db,
        current_user_id=current_user_id,
        sort=sort,
        limit=limit,
        offset=offset,
    )
