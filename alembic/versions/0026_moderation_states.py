"""Add moderation_state columns and active-report uniqueness.

Revision ID: 0026_moderation_states
Revises: 0025_detail_link_sever_requests
Create Date: 2026-08-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_moderation_states"
down_revision = "0025_detail_link_sever_requests"
branch_labels = None
depends_on = None


CONTENT_TABLES = (
    "posts",
    "threads",
    "projects",
    "events",
    "help_requests",
    "comments",
    "messages",
)


def upgrade() -> None:
    for table_name in CONTENT_TABLES:
        op.add_column(
            table_name,
            sa.Column(
                "moderation_state",
                sa.String(length=24),
                nullable=False,
                server_default="visible",
            ),
        )
        op.add_column(
            table_name,
            sa.Column("moderation_reason", sa.String(length=24), nullable=True),
        )
        op.create_index(
            f"ix_{table_name}_moderation_state",
            table_name,
            ["moderation_state"],
            unique=False,
        )

    op.drop_constraint("uq_reports_target", "reports", type_="unique")
    op.create_index(
        "uq_reports_active_target",
        "reports",
        ["target_type", "target_id"],
        unique=True,
        postgresql_where=sa.text("resolution IN ('open', 'under_review', 'hidden')"),
    )


def downgrade() -> None:
    op.drop_index("uq_reports_active_target", table_name="reports")
    op.create_unique_constraint("uq_reports_target", "reports", ["target_type", "target_id"])

    for table_name in CONTENT_TABLES:
        op.drop_index(f"ix_{table_name}_moderation_state", table_name=table_name)
        op.drop_column(table_name, "moderation_reason")
        op.drop_column(table_name, "moderation_state")
