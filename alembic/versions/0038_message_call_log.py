"""Record finished voice calls as messages in the conversation.

Revision ID: 0038_message_call_log
Revises: 0037_project_edit_location
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0038_message_call_log"
down_revision = "0037_project_edit_location"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages", sa.Column("kind", sa.String(16), nullable=False, server_default="text")
    )
    op.add_column("messages", sa.Column("call_id", UUID(as_uuid=True), nullable=True))
    op.add_column("messages", sa.Column("call_outcome", sa.String(16), nullable=True))
    op.add_column("messages", sa.Column("call_duration_seconds", sa.Integer, nullable=True))
    op.create_index("uq_messages_call_id", "messages", ["call_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_messages_call_id", table_name="messages")
    op.drop_column("messages", "call_duration_seconds")
    op.drop_column("messages", "call_outcome")
    op.drop_column("messages", "call_id")
    op.drop_column("messages", "kind")
