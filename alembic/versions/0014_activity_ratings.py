"""Add project and event activity ratings tables

Revision ID: 0014_activity_ratings
Revises: 0013_activity_is_online
Create Date: 2026-07-11
"""

from alembic import op
import sqlalchemy as sa
from app.models.base import UUID

revision = "0014_activity_ratings"
down_revision = "0013_activity_is_online"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_activity_ratings",
        sa.Column("activity_id", UUID, sa.ForeignKey("project_activities.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="project_activity_ratings_rating_range"),
    )
    op.create_table(
        "event_activity_ratings",
        sa.Column("activity_id", UUID, sa.ForeignKey("event_activities.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rating >= 1 AND rating <= 5", name="event_activity_ratings_rating_range"),
    )


def downgrade() -> None:
    op.drop_table("event_activity_ratings")
    op.drop_table("project_activity_ratings")
