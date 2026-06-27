from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import searchable_documents

SEARCHABLE_ENTITY_TYPES = frozenset(
    {"project", "thread", "event", "channel", "community", "user"}
)


def _serialize_search_document(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "id": row["id"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "title": row["title"],
        "summary": row["summary"],
        "meta": row["meta"],
        "href": row["href"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "rank": float(row["rank"]) if row.get("rank") is not None else 0.0,
    }


def _normalize_entity_types(entity_types: Sequence[str] | None) -> list[str]:
    if entity_types is None:
        return []

    normalized = []
    seen = set()
    for raw in entity_types:
        value = raw.strip().lower()
        if not value or value in seen:
            continue
        if value not in SEARCHABLE_ENTITY_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"entity_types must be within: {sorted(SEARCHABLE_ENTITY_TYPES)}",
            )
        seen.add(value)
        normalized.append(value)
    return normalized


def index_document(
    db: Session,
    entity_type: str,
    entity_id: UUID,
    title: str,
    summary: str,
    meta: str,
    href: str,
) -> dict[str, object]:
    """Internal helper to upsert a searchable document when content changes."""
    normalized_entity_type = entity_type.strip().lower()
    if normalized_entity_type not in SEARCHABLE_ENTITY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"entity_type must be one of: {sorted(SEARCHABLE_ENTITY_TYPES)}",
        )

    cleaned_title = title.strip()
    cleaned_summary = summary.strip()
    cleaned_meta = meta.strip()
    cleaned_href = href.strip()

    if not cleaned_title:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="title is required")
    if not cleaned_summary:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="summary is required")
    if not cleaned_meta:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="meta is required")
    if not cleaned_href:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="href is required")

    search_text = " ".join([cleaned_title, cleaned_summary, cleaned_meta])

    insert_stmt = pg_insert(searchable_documents).values(
        entity_type=normalized_entity_type,
        entity_id=entity_id,
        title=cleaned_title,
        summary=cleaned_summary,
        meta=cleaned_meta,
        href=cleaned_href,
        search_vector=func.to_tsvector("english", search_text),
    )

    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[searchable_documents.c.entity_type, searchable_documents.c.entity_id],
        set_={
            "title": cleaned_title,
            "summary": cleaned_summary,
            "meta": cleaned_meta,
            "href": cleaned_href,
            "search_vector": func.to_tsvector("english", search_text),
            "updated_at": func.now(),
        },
    ).returning(
        searchable_documents.c.id,
        searchable_documents.c.entity_type,
        searchable_documents.c.entity_id,
        searchable_documents.c.title,
        searchable_documents.c.summary,
        searchable_documents.c.meta,
        searchable_documents.c.href,
        searchable_documents.c.created_at,
        searchable_documents.c.updated_at,
    )

    try:
        row = db.execute(upsert_stmt).mappings().one()
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not index searchable document",
        ) from exc

    payload = dict(row)
    payload["rank"] = 0.0
    return {"document": _serialize_search_document(payload)}


def search_documents(
    db: Session,
    query: str,
    entity_types: Sequence[str] | None = None,
    limit: int = 20,
) -> dict[str, object]:
    cleaned_query = query.strip()
    if not cleaned_query:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="query is required")

    normalized_types = _normalize_entity_types(entity_types)
    safe_limit = max(1, min(limit, 100))

    ts_query = func.websearch_to_tsquery("english", cleaned_query)
    rank_expr = func.ts_rank_cd(searchable_documents.c.search_vector, ts_query).label("rank")

    stmt = (
        select(
            searchable_documents.c.id,
            searchable_documents.c.entity_type,
            searchable_documents.c.entity_id,
            searchable_documents.c.title,
            searchable_documents.c.summary,
            searchable_documents.c.meta,
            searchable_documents.c.href,
            searchable_documents.c.created_at,
            searchable_documents.c.updated_at,
            rank_expr,
        )
        .where(searchable_documents.c.search_vector.op("@@")(ts_query))
        .order_by(rank_expr.desc(), searchable_documents.c.updated_at.desc())
        .limit(safe_limit)
    )

    if normalized_types:
        stmt = stmt.where(searchable_documents.c.entity_type.in_(normalized_types))

    rows = db.execute(stmt).mappings().all()
    items = [_serialize_search_document(row) for row in rows]

    if len(items) < safe_limit and len(cleaned_query) >= 2:
        existing_ids = {str(item["id"]) for item in items}
        pattern = f"%{cleaned_query}%"
        fallback_stmt = (
            select(
                searchable_documents.c.id,
                searchable_documents.c.entity_type,
                searchable_documents.c.entity_id,
                searchable_documents.c.title,
                searchable_documents.c.summary,
                searchable_documents.c.meta,
                searchable_documents.c.href,
                searchable_documents.c.created_at,
                searchable_documents.c.updated_at,
                literal(0.0).label("rank"),
            )
            .where(
                or_(
                    searchable_documents.c.title.ilike(pattern),
                    searchable_documents.c.summary.ilike(pattern),
                    searchable_documents.c.meta.ilike(pattern),
                )
            )
            .order_by(searchable_documents.c.updated_at.desc())
            .limit(safe_limit)
        )
        if normalized_types:
            fallback_stmt = fallback_stmt.where(
                searchable_documents.c.entity_type.in_(normalized_types)
            )
        fallback_rows = db.execute(fallback_stmt).mappings().all()
        for row in fallback_rows:
            if str(row["id"]) in existing_ids:
                continue
            items.append(_serialize_search_document(row))
            existing_ids.add(str(row["id"]))
            if len(items) >= safe_limit:
                break

    return {"total": len(items), "items": items}