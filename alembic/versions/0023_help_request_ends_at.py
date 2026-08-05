"""Add help_requests.ends_at for schedule ranges.

Revision ID: 0023_help_request_ends_at
Revises: 0022_event_ends_at
Create Date: 2026-07-28
"""

import sqlalchemy as sa

from alembic import op

revision = "0023_help_request_ends_at"
down_revision = "0022_event_ends_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "help_requests",
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("help_requests", "ends_at")
