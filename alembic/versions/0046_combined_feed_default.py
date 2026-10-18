"""One Home feed is the default: existing and new accounts start with feeds combined.

Revision ID: 0046_combined_feed_default
Revises: 0045_booking_provider_role
Create Date: 2026-10-18
"""

import sqlalchemy as sa

from alembic import op

revision = "0046_combined_feed_default"
down_revision = "0045_booking_provider_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("user_settings", "combine_feeds", server_default=sa.true())
    op.execute("UPDATE user_settings SET combine_feeds = true")


def downgrade() -> None:
    op.alter_column("user_settings", "combine_feeds", server_default=sa.false())
