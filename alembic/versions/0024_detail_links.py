"""Generic project/event detail links.

Revision ID: 0024_detail_links
Revises: 0023_help_request_ends_at
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0024_detail_links"
down_revision = "0023_help_request_ends_at"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "detail_links",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "source_project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "source_event_id",
            UUID,
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "target_project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "target_event_id",
            UUID,
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("relationship_label", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("link_kind", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_detail_links_source_project_id", "detail_links", ["source_project_id"], unique=False
    )
    op.create_index(
        "ix_detail_links_source_event_id", "detail_links", ["source_event_id"], unique=False
    )
    op.create_index(
        "ix_detail_links_target_project_id", "detail_links", ["target_project_id"], unique=False
    )
    op.create_index(
        "ix_detail_links_target_event_id", "detail_links", ["target_event_id"], unique=False
    )

    op.create_table(
        "detail_link_requests",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "source_project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "source_event_id",
            UUID,
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "target_project_id",
            UUID,
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "target_event_id",
            UUID,
            sa.ForeignKey("events.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("relationship_label", sa.String(length=120), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "proposed_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_detail_link_requests_source_project_id",
        "detail_link_requests",
        ["source_project_id"],
        unique=False,
    )
    op.create_index(
        "ix_detail_link_requests_source_event_id",
        "detail_link_requests",
        ["source_event_id"],
        unique=False,
    )
    op.create_index(
        "ix_detail_link_requests_target_project_id",
        "detail_link_requests",
        ["target_project_id"],
        unique=False,
    )
    op.create_index(
        "ix_detail_link_requests_target_event_id",
        "detail_link_requests",
        ["target_event_id"],
        unique=False,
    )

    op.create_table(
        "detail_link_request_votes",
        sa.Column(
            "request_id",
            UUID,
            sa.ForeignKey("detail_link_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("vote", sa.String(length=8), nullable=False),
        sa.Column("vote_scope", sa.String(length=16), nullable=False, primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute(
        sa.text(
            """
            insert into detail_links (
                id,
                source_kind,
                source_project_id,
                source_event_id,
                target_kind,
                target_project_id,
                target_event_id,
                relationship_label,
                summary,
                link_kind,
                status,
                created_at
            )
            select
                id,
                'project',
                source_project_id,
                null,
                'project',
                target_project_id,
                null,
                relationship_label,
                summary,
                link_kind,
                status,
                created_at
            from project_links
            """
        )
    )
    op.execute(
        sa.text(
            """
            insert into detail_link_requests (
                id,
                source_kind,
                source_project_id,
                source_event_id,
                target_kind,
                target_project_id,
                target_event_id,
                relationship_label,
                summary,
                proposed_by,
                status,
                created_at
            )
            select
                id,
                'project',
                source_project_id,
                null,
                'project',
                target_project_id,
                null,
                relationship_label,
                summary,
                proposed_by,
                status,
                created_at
            from project_link_requests
            """
        )
    )
    op.execute(
        sa.text(
            """
            insert into detail_link_request_votes (
                request_id,
                voter_id,
                vote,
                vote_scope,
                created_at
            )
            select
                request_id,
                voter_id,
                vote,
                vote_scope,
                created_at
            from project_link_request_votes
            """
        )
    )


def downgrade() -> None:
    op.drop_table("detail_link_request_votes")
    op.drop_index(
        "ix_detail_link_requests_target_event_id", table_name="detail_link_requests"
    )
    op.drop_index(
        "ix_detail_link_requests_target_project_id", table_name="detail_link_requests"
    )
    op.drop_index(
        "ix_detail_link_requests_source_event_id", table_name="detail_link_requests"
    )
    op.drop_index(
        "ix_detail_link_requests_source_project_id", table_name="detail_link_requests"
    )
    op.drop_table("detail_link_requests")
    op.drop_index("ix_detail_links_target_event_id", table_name="detail_links")
    op.drop_index("ix_detail_links_target_project_id", table_name="detail_links")
    op.drop_index("ix_detail_links_source_event_id", table_name="detail_links")
    op.drop_index("ix_detail_links_source_project_id", table_name="detail_links")
    op.drop_table("detail_links")
