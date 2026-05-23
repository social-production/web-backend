from __future__ import annotations

import sqlalchemy as sa

from app.models.base import UUID, created_at, table, updated_at, user_fk, uuid_pk

conversations = table(
    "conversations",
    uuid_pk(),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("title", sa.String(200), nullable=True),
    user_fk("created_by", nullable=True, ondelete="SET NULL"),
    created_at(),
    updated_at(),
    sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
)

conversation_members = table(
    "conversation_members",
    sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
)

messages = table(
    "messages",
    uuid_pk(),
    sa.Column("conversation_id", UUID, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    user_fk("sender_id", nullable=True, ondelete="SET NULL"),
    sa.Column("encrypted_body", sa.Text, nullable=False),
    sa.Column("encryption_version", sa.SmallInteger, nullable=False, server_default="1"),
    created_at(),
    updated_at(),
)
