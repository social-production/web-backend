from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import Float, case, cast, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import content_votes, platform_feedback, users
from app.services.meaningful_actions import record_meaningful_action

VALID_FEEDBACK_KINDS = frozenset({"bug", "suggestion"})
VALID_FEEDBACK_FILTERS = frozenset({"all", "bugs", "suggestions"})
VALID_FEEDBACK_SORTS = frozenset({"trending", "recent"})


def _ensure_platform_feedback_table(db: Session) -> None:
    bind = db.get_bind()
    platform_feedback.create(bind=bind, checkfirst=True)
    if bind.dialect.name == "postgresql":
        db.execute(text("ALTER TABLE content_votes ALTER COLUMN target_type TYPE VARCHAR(24)"))


def _normalize_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    normalized = kind.strip().lower()
    if normalized not in VALID_FEEDBACK_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"kind must be one of: {sorted(VALID_FEEDBACK_KINDS)}",
        )
    return normalized


def _normalize_filter(filter_value: str | None) -> str:
    normalized = (filter_value or "all").strip().lower() or "all"
    if normalized not in VALID_FEEDBACK_FILTERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"filter must be one of: {sorted(VALID_FEEDBACK_FILTERS)}",
        )
    return normalized


def _normalize_sort(sort_value: str | None) -> str:
    normalized = (sort_value or "trending").strip().lower() or "trending"
    if normalized not in VALID_FEEDBACK_SORTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"sort must be one of: {sorted(VALID_FEEDBACK_SORTS)}",
        )
    return normalized


def _vote_fields(row: Mapping[str, object]) -> dict[str, object]:
    upvote_count = int(row.get("upvote_count", 0) or 0)
    downvote_count = int(row.get("downvote_count", 0) or 0)
    total_votes = upvote_count + downvote_count
    approval_percent = (upvote_count / total_votes * 100.0) if total_votes > 0 else 0.0
    active_vote_value = row.get("active_vote")
    active_vote = (
        "up" if active_vote_value == 1 else "down" if active_vote_value == -1 else "neutral"
    )
    return {
        "upvote_count": upvote_count,
        "downvote_count": downvote_count,
        "approval_percent": approval_percent,
        "active_vote": active_vote,
    }


def _serialize_feedback_row(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "description": row["description"],
        "author_id": row["author_id"],
        "author_username": row.get("author_username", "") or "",
        "vote_count": int(row.get("vote_count", 0) or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        **_vote_fields(row),
    }


def create_feedback(
    db: Session,
    current_user_id: UUID,
    *,
    kind: str,
    title: str,
    description: str,
) -> dict[str, object]:
    _ensure_platform_feedback_table(db)
    normalized_kind = _normalize_kind(kind)

    try:
        created = (
            db.execute(
                insert(platform_feedback)
                .values(
                    author_id=current_user_id,
                    kind=normalized_kind,
                    title=title.strip(),
                    description=description.strip(),
                )
                .returning(
                    platform_feedback.c.id,
                    platform_feedback.c.kind,
                    platform_feedback.c.title,
                    platform_feedback.c.description,
                    platform_feedback.c.author_id,
                    platform_feedback.c.vote_count,
                    platform_feedback.c.created_at,
                    platform_feedback.c.updated_at,
                )
            )
            .mappings()
            .one()
        )
        author_username = (
            db.execute(
                select(users.c.username).where(users.c.id == current_user_id)
            ).scalar_one_or_none()
            or ""
        )
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="create-platform-feedback",
            metadata={"feedback_id": str(created["id"]), "kind": normalized_kind},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create feedback",
        ) from exc

    payload = dict(created)
    payload["author_username"] = author_username
    payload["upvote_count"] = 0
    payload["downvote_count"] = 0
    payload["active_vote"] = 0
    return {"feedback": _serialize_feedback_row(payload)}


def _upvote_count_expr():
    return func.coalesce(func.sum(case((content_votes.c.direction == 1, 1), else_=0)), 0)


def _downvote_count_expr():
    return func.coalesce(func.sum(case((content_votes.c.direction == -1, 1), else_=0)), 0)


def _feedback_last_activity_at():
    last_vote_at = func.max(func.greatest(content_votes.c.created_at, content_votes.c.updated_at))
    return func.greatest(
        platform_feedback.c.created_at,
        func.coalesce(last_vote_at, platform_feedback.c.created_at),
    )


def _feedback_trending_score():
    """Recency-weighted score so stale items fall and new ones stay visible.

    last_activity = latest of created_at and any vote timestamp
    decay = 1 / (1 + hours_since_last_activity / 36)
    base = 4 + least(supports, 50) * 2 - least(opposes, 20)
    score = base * decay
    """
    hours_since_activity = func.greatest(
        func.extract("epoch", func.now() - _feedback_last_activity_at()) / 3600.0,
        0.0,
    )
    decay = 1.0 / (1.0 + hours_since_activity / 36.0)
    base_score = (
        4.0
        + func.least(cast(_upvote_count_expr(), Float), 50.0) * 2.0
        - func.least(cast(_downvote_count_expr(), Float), 20.0)
    )
    return (base_score * decay).label("trending_score")


def _feedback_select(current_user_id: UUID | None):
    vote_filter = (content_votes.c.target_type == "platform_feedback") & (
        content_votes.c.target_id == platform_feedback.c.id
    )
    return (
        select(
            platform_feedback.c.id,
            platform_feedback.c.kind,
            platform_feedback.c.title,
            platform_feedback.c.description,
            platform_feedback.c.author_id,
            platform_feedback.c.vote_count,
            platform_feedback.c.created_at,
            platform_feedback.c.updated_at,
            users.c.username.label("author_username"),
            _upvote_count_expr().label("upvote_count"),
            _downvote_count_expr().label("downvote_count"),
            func.max(
                case(
                    (
                        (content_votes.c.voter_id == current_user_id)
                        if current_user_id is not None
                        else False,
                        content_votes.c.direction,
                    ),
                    else_=0,
                )
            ).label("active_vote"),
            _feedback_trending_score(),
        )
        .select_from(
            platform_feedback.outerjoin(
                users, users.c.id == platform_feedback.c.author_id
            ).outerjoin(content_votes, vote_filter)
        )
        .where(platform_feedback.c.moderation_state == "visible")
        .group_by(
            platform_feedback.c.id,
            platform_feedback.c.kind,
            platform_feedback.c.title,
            platform_feedback.c.description,
            platform_feedback.c.author_id,
            platform_feedback.c.vote_count,
            platform_feedback.c.created_at,
            platform_feedback.c.updated_at,
            users.c.username,
        )
    )


def list_feedback(
    db: Session,
    *,
    current_user_id: UUID | None = None,
    filter_value: str | None = None,
    sort: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, object]:
    _ensure_platform_feedback_table(db)
    normalized_filter = _normalize_filter(filter_value)
    normalized_sort = _normalize_sort(sort)

    query = _feedback_select(current_user_id)
    if normalized_filter == "bugs":
        query = query.where(platform_feedback.c.kind == "bug")
    elif normalized_filter == "suggestions":
        query = query.where(platform_feedback.c.kind == "suggestion")

    order_columns = (
        [platform_feedback.c.created_at.desc()]
        if normalized_sort == "recent"
        else [_feedback_trending_score().desc(), platform_feedback.c.created_at.desc()]
    )

    rows = db.execute(query.order_by(*order_columns).limit(limit).offset(offset)).mappings()

    items = [_serialize_feedback_row(row) for row in rows]
    return {
        "items": items,
        "filters": {"filter": normalized_filter, "sort": normalized_sort},
        "page": {"limit": limit, "offset": offset, "count": len(items)},
    }


def get_feedback_by_id(
    db: Session,
    feedback_id: UUID,
    *,
    current_user_id: UUID | None = None,
) -> dict[str, object]:
    _ensure_platform_feedback_table(db)
    row = (
        db.execute(_feedback_select(current_user_id).where(platform_feedback.c.id == feedback_id))
        .mappings()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    return {"feedback": _serialize_feedback_row(row)}
