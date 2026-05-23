from __future__ import annotations

import sqlalchemy as sa

from app.models.base import UUID, created_at, table, user_fk, uuid_pk

notifications = table(
    "notifications",
    uuid_pk(),
    user_fk("recipient_id", nullable=False, ondelete="CASCADE"),
    user_fk("actor_id", nullable=True, ondelete="SET NULL"),
    sa.Column("kind", sa.String(24), nullable=False),
    sa.Column("surface", sa.String(16), nullable=False),
    sa.Column("subject_type", sa.String(16), nullable=False),
    sa.Column("subject_id", UUID, nullable=False),
    sa.Column("target_id", UUID, nullable=True),
    sa.Column("title", sa.String(240), nullable=False),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("href", sa.Text, nullable=False),
    sa.Column("is_unread", sa.Boolean, nullable=False, server_default=sa.true()),
    created_at(),
    sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
)
