"""Add case-insensitive unique index for usernames.

Revision ID: 0017_canonical_handles
Revises: 0016_event_activity_history
Create Date: 2026-07-18
"""

from alembic import op

revision = "0017_canonical_handles"
down_revision = "0016_event_activity_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username_lower ON users (lower(username))"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_channels_slug_lower ON channels (lower(slug))"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_communities_slug_lower ON communities (lower(slug))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_communities_slug_lower")
    op.execute("DROP INDEX IF EXISTS uq_channels_slug_lower")
    op.execute("DROP INDEX IF EXISTS uq_users_username_lower")
