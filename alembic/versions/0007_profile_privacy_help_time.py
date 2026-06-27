"""profile privacy + help request needed_at

Revision ID: 0007_profile_privacy_help_time
Revises: 0006_help_request_tags
Create Date: 2026-06-26 00:00:05.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_profile_privacy_help_time"
down_revision = "0006_help_request_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "hide_public_profile_activity_from_non_followers",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "help_requests",
        sa.Column("needed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE help_requests SET needed_at = created_at WHERE needed_at IS NULL"
    )
    op.alter_column("help_requests", "needed_at", nullable=False)


def downgrade() -> None:
    op.drop_column("help_requests", "needed_at")
    op.drop_column("user_settings", "hide_public_profile_activity_from_non_followers")
