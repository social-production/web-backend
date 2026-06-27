"""help request roles and assignments

Revision ID: 0008_help_request_roles
Revises: 0007_profile_privacy_help_time
Create Date: 2026-06-26 00:00:06.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_help_request_roles"
down_revision = "0007_profile_privacy_help_time"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "help_request_roles",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "help_request_id",
            UUID,
            sa.ForeignKey("help_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("slots", sa.Integer(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_help_request_roles_help_request_id",
        "help_request_roles",
        ["help_request_id"],
    )

    op.create_table(
        "help_request_role_assignments",
        sa.Column(
            "role_id",
            UUID,
            sa.ForeignKey("help_request_roles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, roles FROM help_requests")).fetchall()
    for help_request_id, roles_json in rows:
        roles = roles_json if isinstance(roles_json, list) else json.loads(roles_json or "[]")
        for index, role in enumerate(roles):
            if not isinstance(role, dict):
                continue
            title = str(role.get("title", "")).strip()
            if not title:
                continue
            description = str(role.get("description", "")).strip()
            slots = role.get("slots", 0)
            try:
                slots_int = int(slots)
            except (TypeError, ValueError):
                slots_int = 0
            conn.execute(
                sa.text(
                    """
                    INSERT INTO help_request_roles
                        (help_request_id, title, description, slots, sort_order)
                    VALUES
                        (:help_request_id, :title, :description, :slots, :sort_order)
                    """
                ),
                {
                    "help_request_id": help_request_id,
                    "title": title,
                    "description": description,
                    "slots": slots_int,
                    "sort_order": index,
                },
            )


def downgrade() -> None:
    op.drop_table("help_request_role_assignments")
    op.drop_table("help_request_roles")
