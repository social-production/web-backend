"""Add is_online to event and project activities

Revision ID: 0013_activity_is_online
Revises: 0012_display_timezone
Create Date: 2026-07-08
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_activity_is_online"
down_revision = "0012_display_timezone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_activities",
        sa.Column("is_online", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "project_activities",
        sa.Column("is_online", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("project_activities", "is_online")
    op.drop_column("event_activities", "is_online")
