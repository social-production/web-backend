"""help request scope tags

Revision ID: 0006_help_request_tags
Revises: 0005_user_settings_feed_defaults
Create Date: 2026-06-26 00:00:04.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0006_help_request_tags"
down_revision = "0005_user_settings_feed_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "help_request_tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "help_request_id",
            UUID(as_uuid=True),
            sa.ForeignKey("help_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tag_kind", sa.String(16), nullable=False),
        sa.Column("channel_id", UUID(as_uuid=True), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "community_id",
            UUID(as_uuid=True),
            sa.ForeignKey("communities.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "help_request_id",
            "tag_kind",
            "channel_id",
            "community_id",
            name="uq_help_request_tags_tag",
        ),
    )


def downgrade() -> None:
    op.drop_table("help_request_tags")
