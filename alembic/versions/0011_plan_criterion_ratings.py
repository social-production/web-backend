"""plan criterion ratings

Revision ID: 0011_plan_criterion_ratings
Revises: 0010_user_preferred_language
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_plan_criterion_ratings"
down_revision = "0010_user_preferred_language"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_plan_criterion_ratings",
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("project_plans.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("criterion_id", sa.String(length=120), primary_key=True),
        sa.Column("voter_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "event_plan_criterion_ratings",
        sa.Column("plan_id", sa.Uuid(), sa.ForeignKey("event_plans.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("criterion_id", sa.String(length=120), primary_key=True),
        sa.Column("voter_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("event_plan_criterion_ratings")
    op.drop_table("project_plan_criterion_ratings")
