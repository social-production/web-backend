"""user preferred language

Revision ID: 0010_user_preferred_language
Revises: 0009_help_request_vote_counts
Create Date: 2026-06-27 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_user_preferred_language"
down_revision = "0009_help_request_vote_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("preferred_language", sa.String(length=5), nullable=False, server_default="en"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "preferred_language")
