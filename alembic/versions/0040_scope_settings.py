"""Channel and community pictures, community governance mode, and member-voted proposals.

Revision ID: 0040_scope_settings
Revises: 0039_bot_reviews
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0040_scope_settings"
down_revision = "0039_bot_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("channels", "communities"):
        op.add_column(table, sa.Column("image_storage_key", sa.Text, nullable=True))
        op.add_column(table, sa.Column("image_content_type", sa.String(127), nullable=True))
    op.add_column(
        "communities",
        sa.Column("governance", sa.String(16), nullable=False, server_default="collaborative"),
    )
    op.create_table(
        "scope_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "proposer_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_scope_proposals_scope", "scope_proposals", ["scope_kind", "scope_id", "status"])
    op.create_table(
        "scope_proposal_votes",
        sa.Column(
            "proposal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scope_proposals.id", ondelete="CASCADE"),
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


def downgrade() -> None:
    op.drop_table("scope_proposal_votes")
    op.drop_index("ix_scope_proposals_scope", table_name="scope_proposals")
    op.drop_table("scope_proposals")
    op.drop_column("communities", "governance")
    for table in ("channels", "communities"):
        op.drop_column(table, "image_content_type")
        op.drop_column(table, "image_storage_key")
