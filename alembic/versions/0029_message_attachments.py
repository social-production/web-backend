"""Message attachment blobs and conversation pins.

Revision ID: 0029_message_attachments
Revises: 0028_notification_prefs
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0029_message_attachments"
down_revision = "0028_notification_prefs"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "blobs",
        sa.Column("storage_key", sa.Text(), primary_key=True),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "message_attachments",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "message_id",
            UUID,
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "storage_key",
            sa.Text(),
            sa.ForeignKey("blobs.storage_key"),
            nullable=False,
        ),
        sa.UniqueConstraint("message_id", name="uq_message_attachments_message_id"),
        sa.CheckConstraint("kind IN ('image', 'file')", name="ck_message_attachments_kind"),
    )
    op.create_table(
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
        sa.Column("pinned_by", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "pinned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("conversation_pins")
    op.drop_table("message_attachments")
    op.drop_table("blobs")
