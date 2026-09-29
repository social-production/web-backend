"""Comment attachments for project, event, and help-request chats.

Revision ID: 0030_comment_attachments
Revises: 0029_message_attachments
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0030_comment_attachments"
down_revision = "0029_message_attachments"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "comment_attachments",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "comment_id",
            UUID,
            sa.ForeignKey("comments.id", ondelete="CASCADE"),
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
        sa.UniqueConstraint("comment_id", name="uq_comment_attachments_comment_id"),
        sa.CheckConstraint("kind IN ('image', 'file')", name="ck_comment_attachments_kind"),
    )


def downgrade() -> None:
    op.drop_table("comment_attachments")
