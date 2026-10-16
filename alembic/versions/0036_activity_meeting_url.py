"""Give online activities a meeting link members use to join.

Revision ID: 0036_activity_meeting_url
Revises: 0035_svc_request_decline_reason
Create Date: 2026-10-16
"""

import sqlalchemy as sa

from alembic import op

revision = "0036_activity_meeting_url"
down_revision = "0035_svc_request_decline_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("project_activities", sa.Column("meeting_url", sa.String(500), nullable=True))
    op.add_column("event_activities", sa.Column("meeting_url", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("event_activities", "meeting_url")
    op.drop_column("project_activities", "meeting_url")
