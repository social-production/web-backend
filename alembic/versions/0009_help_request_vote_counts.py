"""help request vote and comment counts

Revision ID: 0009_help_request_vote_counts
Revises: 0008_help_request_roles
Create Date: 2026-06-27 00:00:01.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_help_request_vote_counts"
down_revision = "0008_help_request_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "help_requests",
        sa.Column("vote_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "help_requests",
        sa.Column("comment_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("help_requests", "comment_count")
    op.drop_column("help_requests", "vote_count")
