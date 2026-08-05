from __future__ import annotations

import secrets
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Table


def slugify(title: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")
    return "-".join(part for part in cleaned.split("-") if part)[:80] or "item"


def slug_exists(db: Session, table: Table, candidate: str) -> bool:
    return db.execute(select(table.c.id).where(table.c.slug == candidate)).first() is not None


def allocate_unique_slug(db: Session, table: Table, base_title: str) -> str:
    base = slugify(base_title)
    for _ in range(10):
        candidate = f"{base}-{secrets.token_hex(4)}"
        if not slug_exists(db, table, candidate):
            return candidate
    return f"{base}-{uuid4().hex[:12]}"
