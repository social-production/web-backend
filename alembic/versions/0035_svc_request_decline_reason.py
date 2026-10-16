"""Store an optional reason when a service request is declined.

Revision ID: 0035_svc_request_decline_reason
Revises: 0034_chat_actions
Create Date: 2026-10-09
"""

import sqlalchemy as sa

from alembic import op

revision = "0035_svc_request_decline_reason"
down_revision = "0034_chat_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_service_requests",
        sa.Column("decline_reason", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_service_requests", "decline_reason")
