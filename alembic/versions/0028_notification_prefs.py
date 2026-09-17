"""Notification category preferences on user settings.

Revision ID: 0028_notification_prefs
Revises: 0027_avail_and_roles
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0028_notification_prefs"
down_revision = "0027_avail_and_roles"
branch_labels = None
depends_on = None

DEFAULT_CATEGORIES = '["follows","comments","shares_invites","roles","votes_needed","phase_done"]'


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "notification_categories",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text(f"'{DEFAULT_CATEGORIES}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "notification_categories")
