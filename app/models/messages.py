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
    sa.Column(
        "conversation_id",
        UUID,
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=True),
)

messages = table(
    "messages",
    uuid_pk(),
    sa.Column(
        "conversation_id",
        UUID,
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    user_fk("sender_id", nullable=True, ondelete="SET NULL"),
    sa.Column("encrypted_body", sa.Text, nullable=False),
    sa.Column("encryption_version", sa.SmallInteger, nullable=False, server_default="1"),
    sa.Column("moderation_state", sa.String(24), nullable=False, server_default="visible"),
    sa.Column("moderation_reason", sa.String(24), nullable=True),
    created_at(),
    updated_at(),
)

subject_chat_reads = table(
    "subject_chat_reads",
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("subject_type", sa.String(length=16), primary_key=True),
    sa.Column("subject_id", UUID, primary_key=True),
    sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
)

blobs = table(
    "blobs",
    sa.Column("storage_key", sa.Text, primary_key=True),
    sa.Column("ciphertext", sa.LargeBinary, nullable=False),
    created_at(),
)

message_attachments = table(
    "message_attachments",
    uuid_pk(),
    sa.Column(
        "message_id",
        UUID,
        sa.ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("filename", sa.String(200), nullable=False),
    sa.Column("content_type", sa.String(127), nullable=False),
    sa.Column("byte_size", sa.Integer, nullable=False),
    sa.Column("storage_key", sa.Text, sa.ForeignKey("blobs.storage_key"), nullable=False),
    sa.Index("ix_message_attachments_message_id", "message_id"),
    sa.CheckConstraint("kind IN ('image', 'file')", name="ck_message_attachments_kind"),
)

conversation_pins = table(
    "conversation_pins",
    sa.Column(
        "conversation_id",
        UUID,
        sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "message_id",
        UUID,
        sa.ForeignKey("messages.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    user_fk("pinned_by", nullable=False, ondelete="CASCADE"),
    sa.Column(
        "pinned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
