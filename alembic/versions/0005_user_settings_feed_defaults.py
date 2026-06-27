"""user settings feed scope defaults

Revision ID: 0005_user_settings_feed_defaults
Revises: 0004_help_requests
Create Date: 2026-06-26 00:00:03.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_user_settings_feed_defaults"
down_revision = "0004_help_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "user_settings",
        "public_feed_scope",
        server_default=sa.text("'global'"),
    )
    op.alter_column(
        "user_settings",
        "personal_feed_scope",
        server_default=sa.text("'popular'"),
    )


def downgrade() -> None:
    op.alter_column(
        "user_settings",
        "public_feed_scope",
        server_default=sa.text("'home'"),
    )
    op.alter_column(
        "user_settings",
        "personal_feed_scope",
        server_default=sa.text("'following'"),
    )
