"""Account-to-account vouch and bot stances.

Revision ID: 0032_account_stances
Revises: 0031_multiple_attachments
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0032_account_stances"
down_revision = "0031_multiple_attachments"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "account_stances",
        sa.Column(
            "source_user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "target_user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("stance", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("source_user_id <> target_user_id", name="ck_account_stances_not_self"),
        sa.CheckConstraint("stance IN ('vouch', 'bot')", name="ck_account_stances_kind"),
    )
    op.create_index(
        "ix_account_stances_target_user_id",
        "account_stances",
        ["target_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_account_stances_target_user_id", table_name="account_stances")
    op.drop_table("account_stances")
