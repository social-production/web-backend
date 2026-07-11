"""Add participant_auto_uncompleted_at to project activities

Revision ID: 0015_participant_uncompleted
Revises: 0014_activity_ratings
Create Date: 2026-07-11
"""

from alembic import op
import sqlalchemy as sa

revision = "0015_participant_uncompleted"
down_revision = "0014_activity_ratings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_activities",
        sa.Column("participant_auto_uncompleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_activities", "participant_auto_uncompleted_at")
