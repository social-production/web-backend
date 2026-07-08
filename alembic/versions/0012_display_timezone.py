"""Add display_timezone to user_settings

Revision ID: 0012_display_timezone
Revises: 0011_plan_criterion_ratings
Create Date: 2026-07-06
"""

from alembic import op
import sqlalchemy as sa

revision = "0012_display_timezone"
down_revision = "0011_plan_criterion_ratings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("display_timezone", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "display_timezone")
