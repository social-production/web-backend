"""Add events.ends_at for scheduled event end times.

Revision ID: 0022_event_ends_at
Revises: 0021_project_conversion
Create Date: 2026-07-22
"""

import sqlalchemy as sa

from alembic import op

revision = "0022_event_ends_at"
down_revision = "0021_project_conversion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("events", "ends_at")
