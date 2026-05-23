from __future__ import annotations

import sqlalchemy as sa

from app.models.base import TSVECTOR, UUID, created_at, table, updated_at, uuid_pk

searchable_documents = table(
    "searchable_documents",
    uuid_pk(),
    sa.Column("entity_type", sa.String(24), nullable=False),
    sa.Column("entity_id", UUID, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("meta", sa.Text, nullable=False),
    sa.Column("href", sa.Text, nullable=False),
    sa.Column("search_vector", TSVECTOR, nullable=False),
    created_at(),
    updated_at(),
    sa.UniqueConstraint("entity_type", "entity_id", name="uq_searchable_documents_entity"),
)
