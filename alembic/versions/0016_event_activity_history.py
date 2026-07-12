"""Add event activity history completions and auto-uncompleted marker

Revision ID: 0016_event_activity_history
Revises: 0015_participant_uncompleted
Create Date: 2026-07-11
"""

from alembic import op
import sqlalchemy as sa

revision = "0016_event_activity_history"
down_revision = "0015_participant_uncompleted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "event_activities",
        sa.Column("participant_auto_uncompleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "event_activity_history_completions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("history_item_key", sa.String(length=120), nullable=False),
        sa.Column("participant_user_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("completion_state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id",
            "history_item_key",
            "role",
            "participant_user_id",
            name="uq_event_activity_history_completions_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("event_activity_history_completions")
    op.drop_column("event_activities", "participant_auto_uncompleted_at")
