"""Allow multiple attachments on one message or comment.

Revision ID: 0031_multiple_attachments
Revises: 0030_comment_attachments
Create Date: 2026-09-29
"""

from __future__ import annotations

from alembic import op

revision = "0031_multiple_attachments"
down_revision = "0030_comment_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_comment_attachments_comment_id", "comment_attachments", type_="unique")
    op.create_index("ix_comment_attachments_comment_id", "comment_attachments", ["comment_id"])
    op.drop_constraint("uq_message_attachments_message_id", "message_attachments", type_="unique")
    op.create_index("ix_message_attachments_message_id", "message_attachments", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_message_attachments_message_id", table_name="message_attachments")
    op.create_unique_constraint(
        "uq_message_attachments_message_id", "message_attachments", ["message_id"]
    )
    op.drop_index("ix_comment_attachments_comment_id", table_name="comment_attachments")
    op.create_unique_constraint(
        "uq_comment_attachments_comment_id", "comment_attachments", ["comment_id"]
    )
