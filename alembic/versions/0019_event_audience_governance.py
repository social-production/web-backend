"""Add event audience/governance model for private and organizer-controlled events.

Introduces `events.audience` (public | private_community | invite_only) and
`events.governance` (collaborative | organizer_controlled), plus
`events.home_community_id` for private-community events. Backfills existing
rows from `is_private`/tags and keeps `is_private` in sync as a compatibility
field (`is_private = audience != 'public'`).

Revision ID: 0019_event_audience_governance
Revises: 0018_feed_engagement_signals
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_event_audience_governance"
down_revision = "0018_feed_engagement_signals"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("audience", sa.String(24), nullable=False, server_default="public"),
    )
    op.add_column(
        "events",
        sa.Column("governance", sa.String(24), nullable=False, server_default="collaborative"),
    )
    op.add_column(
        "events",
        sa.Column("home_community_id", UUID, nullable=True),
    )
    op.create_foreign_key(
        "fk_events_home_community_id",
        "events",
        "communities",
        ["home_community_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_events_audience",
        "events",
        "audience IN ('public', 'private_community', 'invite_only')",
    )
    op.create_check_constraint(
        "ck_events_governance",
        "events",
        "governance IN ('collaborative', 'organizer_controlled')",
    )

    # Backfill previously-private events. Pre-Phase-4, all private events were
    # already creator-controlled by design (see governance-rules.md), so they
    # backfill as organizer_controlled. Public events remain collaborative.
    op.execute(
        """
        UPDATE events
        SET governance = 'organizer_controlled',
            audience = 'invite_only'
        WHERE is_private = TRUE
        """
    )

    # Events tagged to exactly one community while private are treated as
    # private-community events scoped to that community.
    op.execute(
        """
        WITH single_community_tags AS (
            SELECT event_id, (array_agg(community_id))[1] AS community_id
            FROM event_tags
            WHERE tag_kind = 'community' AND community_id IS NOT NULL
            GROUP BY event_id
            HAVING COUNT(DISTINCT community_id) = 1
        )
        UPDATE events
        SET audience = 'private_community',
            home_community_id = single_community_tags.community_id
        FROM single_community_tags
        WHERE events.id = single_community_tags.event_id
          AND events.is_private = TRUE
        """
    )


def downgrade() -> None:
    op.drop_constraint("ck_events_governance", "events", type_="check")
    op.drop_constraint("ck_events_audience", "events", type_="check")
    op.drop_constraint("fk_events_home_community_id", "events", type_="foreignkey")
    op.drop_column("events", "home_community_id")
    op.drop_column("events", "governance")
    op.drop_column("events", "audience")
