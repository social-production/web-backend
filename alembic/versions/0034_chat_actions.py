"""Chat list pin, mute, and hide, plus message reply and edit.

Revision ID: 0034_chat_actions
Revises: 0033_combine_feeds
Create Date: 2026-10-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0034_chat_actions"
down_revision = "0033_combine_feeds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_members", sa.Column("list_pinned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversation_members", sa.Column("muted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("conversation_members", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("reply_to_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("messages", sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_messages_reply_to_id",
        "messages",
        "messages",
        ["reply_to_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("subject_chat_reads", sa.Column("list_pinned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subject_chat_reads", sa.Column("muted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subject_chat_reads", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("subject_chat_reads", "hidden_at")
    op.drop_column("subject_chat_reads", "muted_at")
    op.drop_column("subject_chat_reads", "list_pinned_at")
    op.drop_constraint("fk_messages_reply_to_id", "messages", type_="foreignkey")
    op.drop_column("messages", "edited_at")
    op.drop_column("messages", "reply_to_id")
    op.drop_column("conversation_members", "hidden_at")
    op.drop_column("conversation_members", "muted_at")
    op.drop_column("conversation_members", "list_pinned_at")
