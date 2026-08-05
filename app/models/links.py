from __future__ import annotations

import sqlalchemy as sa

from app.models.base import UUID, created_at, event_fk, project_fk, table, user_fk, uuid_pk

LINK_SUBJECT_KINDS = ("project", "event")
LINK_KINDS = ("manual", "conversion")
LINK_STATUSES = ("active", "inactive")
LINK_REQUEST_STATUSES = ("open", "approved", "rejected")
LINK_REQUEST_TYPES = ("create", "sever")


detail_links = table(
    "detail_links",
    uuid_pk(),
    sa.Column("source_kind", sa.String(24), nullable=False),
    project_fk("source_project_id", nullable=True, ondelete="CASCADE"),
    event_fk("source_event_id", nullable=True, ondelete="CASCADE"),
    sa.Column("target_kind", sa.String(24), nullable=False),
    project_fk("target_project_id", nullable=True, ondelete="CASCADE"),
    event_fk("target_event_id", nullable=True, ondelete="CASCADE"),
    sa.Column("relationship_label", sa.String(120), nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("link_kind", sa.String(24), nullable=False),
    sa.Column("status", sa.String(24), nullable=False, server_default="active"),
    created_at(),
)


detail_link_requests = table(
    "detail_link_requests",
    uuid_pk(),
    sa.Column("source_kind", sa.String(24), nullable=False),
    project_fk("source_project_id", nullable=True, ondelete="CASCADE"),
    event_fk("source_event_id", nullable=True, ondelete="CASCADE"),
    sa.Column("target_kind", sa.String(24), nullable=False),
    project_fk("target_project_id", nullable=True, ondelete="CASCADE"),
    event_fk("target_event_id", nullable=True, ondelete="CASCADE"),
    sa.Column("relationship_label", sa.String(120), nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("request_type", sa.String(24), nullable=False, server_default="create"),
    sa.Column(
        "link_id",
        UUID,
        sa.ForeignKey("detail_links.id", ondelete="CASCADE"),
        nullable=True,
    ),
    user_fk("proposed_by", nullable=True, ondelete="SET NULL"),
    sa.Column("status", sa.String(24), nullable=False, server_default="open"),
    created_at(),
)


detail_link_request_votes = table(
    "detail_link_request_votes",
    sa.Column(
        "request_id",
        UUID,
        sa.ForeignKey("detail_link_requests.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("voter_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("vote", sa.String(8), nullable=False),
    sa.Column("vote_scope", sa.String(16), nullable=False, primary_key=True),
    created_at(),
)
