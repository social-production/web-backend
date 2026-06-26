"""subject chat reads

Revision ID: 0002_subject_chat_reads
Revises: 0001_phase1_initial_schema
Create Date: 2026-06-26 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_subject_chat_reads"
down_revision = "0001_phase1_initial_schema"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "subject_chat_reads",
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("subject_type", sa.String(length=16), primary_key=True),
        sa.Column("subject_id", UUID, primary_key=True),
        sa.Column("last_read_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("subject_chat_reads")
