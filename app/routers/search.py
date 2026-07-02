from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user_id
from app.dependencies import get_db
from app.services.search import search_documents

router = APIRouter(prefix="/search", tags=["search"])


class SearchItemOut(BaseModel):
    id: UUID
    entity_type: str
    entity_id: UUID
    title: str
    summary: str
    meta: str
    href: str
    created_at: object
    updated_at: object
    rank: float


class SearchResponse(BaseModel):
    total: int
    items: list[SearchItemOut]


@router.get("", response_model=SearchResponse)
def search(
    q: str = Query(min_length=1, max_length=500),
    entity_types: list[str] | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    current_user_id: UUID = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    return search_documents(
        db=db,
        query=q,
        entity_types=entity_types,
        limit=limit,
        viewer_id=current_user_id,
    )
