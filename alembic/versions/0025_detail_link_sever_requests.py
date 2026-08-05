"""Add request_type and link_id to detail_link_requests for sever votes.

Revision ID: 0025_detail_link_sever_requests
Revises: 0024_detail_links
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0025_detail_link_sever_requests"
down_revision = "0024_detail_links"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column(
        "detail_link_requests",
        sa.Column(
            "request_type",
            sa.String(length=24),
            nullable=False,
            server_default="create",
        ),
    )
    op.add_column(
        "detail_link_requests",
        sa.Column(
            "link_id",
            UUID,
            sa.ForeignKey("detail_links.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_detail_link_requests_link_id",
        "detail_link_requests",
        ["link_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_detail_link_requests_link_id", table_name="detail_link_requests")
    op.drop_column("detail_link_requests", "link_id")
    op.drop_column("detail_link_requests", "request_type")
