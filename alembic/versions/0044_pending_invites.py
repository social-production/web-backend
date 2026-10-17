"""Direct invites wait for the person to accept before they join.

Revision ID: 0044_pending_invites
Revises: 0043_merge_duplicate_values
Create Date: 2026-10-17
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0044_pending_invites"
down_revision = "0043_merge_duplicate_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_invites",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "invited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_pending_invites_open",
        "pending_invites",
        ["scope_kind", "scope_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index("ix_pending_invites_user", "pending_invites", ["user_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_pending_invites_user", table_name="pending_invites")
    op.drop_index("uq_pending_invites_open", table_name="pending_invites")
    op.drop_table("pending_invites")
