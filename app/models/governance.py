from __future__ import annotations

import sqlalchemy as sa

from app.models.base import UUID, created_at, table, updated_at, user_fk

platform_board_memberships = table(
    "platform_board_memberships",
    sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("standing_state", sa.String(24), nullable=False),
    sa.Column("grace_started_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("grace_ends_at", sa.DateTime(timezone=True), nullable=True),
    updated_at(),
)

board_standing_votes = table(
    "board_standing_votes",
    sa.Column(
        "target_user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.SmallInteger, nullable=False),
    created_at(),
    updated_at(),
)

governance_decision_history = table(
    "governance_decision_history",
    sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("entity_kind", sa.String(16), nullable=False),
    sa.Column("entity_id", UUID, nullable=False),
    sa.Column("decision_kind", sa.String(48), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column(
        "approval_threshold_percent", sa.Numeric(5, 2), nullable=False, server_default="66.00"
    ),
    sa.Column("payload", sa.dialects.postgresql.JSONB, nullable=False),
    user_fk("author_id", nullable=True, ondelete="SET NULL"),
    created_at(),
    sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
)
