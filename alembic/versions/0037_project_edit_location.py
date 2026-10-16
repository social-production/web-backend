"""Let a project edit request change the project's location.

Revision ID: 0037_project_edit_location
Revises: 0036_activity_meeting_url
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0037_project_edit_location"
down_revision = "0036_activity_meeting_url"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_edit_requests", sa.Column("location_label", sa.String(160), nullable=True)
    )
    op.add_column(
        "project_edit_requests",
        sa.Column(
            "location_id",
            UUID(as_uuid=True),
            sa.ForeignKey("locations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("project_edit_requests", "location_id")
    op.drop_column("project_edit_requests", "location_label")
