"""Project conversion execution support.

Adds unique predecessor conversion guard, successor title/description on phase
change requests, and immutable inherited decision snapshots for successors.

Revision ID: 0021_project_conversion
Revises: 0020_location_foundation
Create Date: 2026-07-18
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0021_project_conversion"
down_revision = "0020_location_foundation"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "project_phase_change_requests",
        sa.Column("conversion_successor_title", sa.String(200), nullable=True),
    )
    op.add_column(
        "project_phase_change_requests",
        sa.Column("conversion_successor_description", sa.Text(), nullable=True),
    )

    op.create_index(
        "uq_project_conversions_predecessor",
        "project_conversions",
        ["predecessor_project_id"],
        unique=True,
    )

    op.create_table(
        "project_inherited_decisions",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "successor_project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "predecessor_project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("predecessor_slug", sa.String(80), nullable=False),
        sa.Column("predecessor_title", sa.String(200), nullable=False),
        sa.Column("source_decision_id", UUID, nullable=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("kind_label", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("author_username", sa.String(64), nullable=False),
        sa.Column("original_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("electorate_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("yes_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "approval_threshold_percent",
            sa.Integer(),
            nullable=False,
            server_default="66",
        ),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_project_inherited_decisions_successor",
        "project_inherited_decisions",
        ["successor_project_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_inherited_decisions_successor",
        table_name="project_inherited_decisions",
    )
    op.drop_table("project_inherited_decisions")
    op.drop_index("uq_project_conversions_predecessor", table_name="project_conversions")
    op.drop_column("project_phase_change_requests", "conversion_successor_description")
    op.drop_column("project_phase_change_requests", "conversion_successor_title")
