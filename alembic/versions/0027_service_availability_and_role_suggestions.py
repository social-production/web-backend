"""Personal-service availability, slot holds, and activity role suggestions.

Revision ID: 0027_avail_and_roles
Revises: 0026_moderation_states
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0027_avail_and_roles"
down_revision = "0026_moderation_states"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "project_service_availability_rules",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("starts_on", sa.Date(), nullable=True),
        sa.Column("ends_on", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("weekday >= 0 AND weekday <= 6", name="availability_weekday_range"),
    )
    op.create_index(
        "ix_project_service_availability_rules_project_id",
        "project_service_availability_rules",
        ["project_id"],
    )

    op.create_table(
        "project_service_slot_holds",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "request_id",
            UUID,
            sa.ForeignKey("project_service_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_project_service_slot_holds_project_id",
        "project_service_slot_holds",
        ["project_id"],
    )
    op.create_index(
        "ix_project_service_slot_holds_request_id",
        "project_service_slot_holds",
        ["request_id"],
        unique=True,
    )

    op.add_column(
        "project_service_requests",
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    for table_name in ("project_activity_roles", "event_activity_roles"):
        op.add_column(
            table_name,
            sa.Column(
                "suggested_user_id",
                UUID,
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.add_column(
            table_name,
            sa.Column(
                "suggested_by_user_id",
                UUID,
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.add_column(
            table_name,
            sa.Column("suggestion_status", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    for table_name in ("project_activity_roles", "event_activity_roles"):
        op.drop_column(table_name, "suggestion_status")
        op.drop_column(table_name, "suggested_by_user_id")
        op.drop_column(table_name, "suggested_user_id")

    op.drop_column("project_service_requests", "conversation_id")
    op.drop_index("ix_project_service_slot_holds_request_id", table_name="project_service_slot_holds")
    op.drop_index("ix_project_service_slot_holds_project_id", table_name="project_service_slot_holds")
    op.drop_table("project_service_slot_holds")
    op.drop_index(
        "ix_project_service_availability_rules_project_id",
        table_name="project_service_availability_rules",
    )
    op.drop_table("project_service_availability_rules")
