"""Retire content votes on projects/events in favor of support/oppose signals.

Revision ID: 0018_feed_engagement_signals
Revises: 0017_canonical_handles
Create Date: 2026-07-18
"""

from alembic import op

revision = "0018_feed_engagement_signals"
down_revision = "0017_canonical_handles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM content_votes WHERE target_type IN ('project', 'event')")
    op.execute("UPDATE projects SET vote_count = 0")
    op.execute("UPDATE events SET vote_count = 0")


def downgrade() -> None:
    # Vote counts and the deleted content_votes rows for project/event targets
    # cannot be reconstructed; this migration is intentionally not reversible.
    pass
