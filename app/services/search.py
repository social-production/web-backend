from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from sqlalchemy import func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Table

from app.errors import InternalAppError, ValidationAppError
from app.models import channels, communities, searchable_documents, users
from app.services.access_control import filter_search_results
from app.unit_of_work import commit, rollback

SEARCHABLE_ENTITY_TYPES = frozenset(
    {"project", "thread", "event", "channel", "community", "user", "help_request", "post"}
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
            raise ValidationAppError(
                f"entity_types must be within: {sorted(SEARCHABLE_ENTITY_TYPES)}"
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
        raise ValidationAppError(f"entity_type must be one of: {sorted(SEARCHABLE_ENTITY_TYPES)}")

    cleaned_title = title.strip()
    cleaned_summary = summary.strip()
    cleaned_meta = meta.strip()
    cleaned_href = href.strip()

    if not cleaned_title:
        raise ValidationAppError("title is required")
    if not cleaned_summary:
        raise ValidationAppError("summary is required")
    if not cleaned_meta:
        raise ValidationAppError("meta is required")
    if not cleaned_href:
        raise ValidationAppError("href is required")

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
        commit(db)
    except IntegrityError as exc:
        rollback(db)
        raise InternalAppError("Could not index searchable document") from exc

    payload = dict(row)
    payload["rank"] = 0.0
    return {"document": _serialize_search_document(payload)}


def search_documents(
    db: Session,
    query: str,
    entity_types: Sequence[str] | None = None,
    limit: int = 20,
    viewer_id: UUID | None = None,
) -> dict[str, object]:
    cleaned_query = query.strip()
    if not cleaned_query:
        raise ValidationAppError("query is required")

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

    _merge_account_matches(db, cleaned_query, items, safe_limit, normalized_types)
    _merge_scope_matches(db, cleaned_query, items, safe_limit, normalized_types)
    filtered_items = filter_search_results(db, viewer_id, items)
    return {"total": len(filtered_items), "items": filtered_items}


def _merge_account_matches(
    db: Session,
    query: str,
    items: list[dict[str, object]],
    limit: int,
    entity_types: list[str],
) -> None:
    """Accounts are searchable by username even when they were never indexed."""
    if entity_types and "user" not in entity_types:
        return

    pattern = f"%{query}%"
    seen = {item["entity_id"] for item in items if item.get("entity_type") == "user"}
    rows = db.execute(
        select(
            users.c.id,
            users.c.username,
            users.c.bio,
            users.c.created_at,
            users.c.updated_at,
        )
        .where(
            users.c.is_active.is_(True),
            or_(
                users.c.username.ilike(pattern),
                func.coalesce(users.c.bio, "").ilike(pattern),
            ),
        )
        .order_by(users.c.username.asc())
        .limit(limit)
    ).all()
    needle = query.casefold()
    matches: list[dict[str, object]] = []
    for user_id, username, bio, created_at, updated_at in rows:
        if user_id in seen:
            continue
        name = str(username)
        folded = name.casefold()
        if folded == needle:
            rank = 2.0
        elif folded.startswith(needle):
            rank = 1.0
        else:
            rank = 0.2
        matches.append(
            {
                "id": user_id,
                "entity_type": "user",
                "entity_id": user_id,
                "title": name,
                "summary": bio or name,
                "meta": "user",
                "href": f"/profile/{name}",
                "created_at": created_at,
                "updated_at": updated_at,
                "rank": rank,
            }
        )
    matches.sort(key=lambda item: (-float(item["rank"]), str(item["title"]).casefold()))
    items[:0] = matches
    if len(items) > limit:
        del items[limit:]


def _scope_match_rank(name: str, slug: str, needle: str) -> float:
    folded_name = name.casefold()
    folded_slug = slug.casefold()
    if needle in (folded_name, folded_slug):
        return 2.0
    if folded_name.startswith(needle) or folded_slug.startswith(needle):
        return 1.0
    return 0.2


def _merge_scope_matches(
    db: Session,
    query: str,
    items: list[dict[str, object]],
    limit: int,
    entity_types: list[str],
) -> None:
    """Channels and communities stay searchable even when they were never indexed."""
    tables: list[tuple[str, Table, str]] = []
    if not entity_types or "channel" in entity_types:
        tables.append(("channel", channels, "/channels"))
    if not entity_types or "community" in entity_types:
        tables.append(("community", communities, "/communities"))
    if not tables or len(query) < 2:
        return

    pattern = f"%{query}%"
    needle = query.casefold()
    matches: list[dict[str, object]] = []
    for entity_type, table, prefix in tables:
        seen = {item["entity_id"] for item in items if item.get("entity_type") == entity_type}
        rows = db.execute(
            select(
                table.c.id,
                table.c.slug,
                table.c.name,
                table.c.description,
                table.c.created_at,
                table.c.updated_at,
            )
            .where(or_(table.c.name.ilike(pattern), table.c.slug.ilike(pattern)))
            .order_by(table.c.name.asc())
            .limit(limit)
        ).all()
        for scope_id, slug, name, description, created_at, updated_at in rows:
            if scope_id in seen:
                continue
            title = str(name)
            matches.append(
                {
                    "id": scope_id,
                    "entity_type": entity_type,
                    "entity_id": scope_id,
                    "title": title,
                    "summary": description or title,
                    "meta": entity_type,
                    "href": f"{prefix}/{slug}",
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "rank": _scope_match_rank(title, str(slug), needle),
                }
            )
    matches.sort(key=lambda item: (-float(item["rank"]), str(item["title"]).casefold()))
    items[:0] = matches
    if len(items) > limit:
        del items[limit:]
