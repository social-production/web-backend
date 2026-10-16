"""Public bot reviews: marks open a review; full members decide.

Revision ID: 0039_bot_reviews
Revises: 0038_message_call_log
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0039_bot_reviews"
down_revision = "0038_message_call_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_reviews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "target_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("is_appeal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bot_reviews_target", "bot_reviews", ["target_user_id", "opened_at"])
    op.create_table(
        "bot_review_votes",
        sa.Column(
            "review_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bot_reviews.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "voter_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("vote", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "bot_mark_cooldowns",
        sa.Column(
            "marker_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "target_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("bot_mark_cooldowns")
    op.drop_table("bot_review_votes")
    op.drop_index("ix_bot_reviews_target", table_name="bot_reviews")
    op.drop_table("bot_reviews")
