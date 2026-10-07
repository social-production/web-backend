"""Add Home-feed and type-size account settings.

Revision ID: 0033_combine_feeds
Revises: 0032_account_stances
Create Date: 2026-10-07
"""

from alembic import op
import sqlalchemy as sa

revision = "0033_combine_feeds"
down_revision = "0032_account_stances"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("combine_feeds", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "user_settings",
        sa.Column("text_size", sa.String(8), nullable=False, server_default="medium"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "text_size")
    op.drop_column("user_settings", "combine_feeds")
