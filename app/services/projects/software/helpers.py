from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    project_memberships,
    project_merge_capability_members,
    projects,
    users,
)
from app.services.projects.software.constants import PR_STAGE_LABELS

_TABLES_READY = False


def _ensure_software_tables(db: Session) -> None:
    global _TABLES_READY
    if _TABLES_READY:
        return

    ddl = [
        """
        CREATE TABLE IF NOT EXISTS project_pull_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            decision_id UUID NULL,
            title VARCHAR(200) NOT NULL,
            summary TEXT NOT NULL,
            pull_request_id VARCHAR(120) NOT NULL,
            pull_request_url TEXT NOT NULL,
            author_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            stage VARCHAR(24) NOT NULL DEFAULT 'approval',
            merge_id VARCHAR(120) NULL,
            merged_by_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            approval_threshold_percent NUMERIC(5, 2) NOT NULL DEFAULT 66.00,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_pull_request_votes (
            request_id UUID NOT NULL REFERENCES project_pull_requests(id) ON DELETE CASCADE,
            voter_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vote VARCHAR(8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (request_id, voter_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_merge_capability_members (
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_label VARCHAR(120) NOT NULL DEFAULT 'approved-request',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (project_id, user_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_merge_capability_change_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            decision_id UUID NOT NULL,
            action VARCHAR(8) NOT NULL,
            target_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            author_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'open',
            approval_threshold_percent NUMERIC(5, 2) NOT NULL DEFAULT 66.00,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_merge_capability_change_votes (
            request_id UUID NOT NULL REFERENCES project_merge_capability_change_requests(id) ON DELETE CASCADE,
            voter_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vote VARCHAR(8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (request_id, voter_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_repository_replacement_requests (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            decision_id UUID NOT NULL,
            repository_url TEXT NOT NULL,
            previous_repository_url TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            related_pull_request_id UUID NOT NULL REFERENCES project_pull_requests(id) ON DELETE CASCADE,
            author_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'open',
            approval_threshold_percent NUMERIC(5, 2) NOT NULL DEFAULT 66.00,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS project_repository_replacement_votes (
            request_id UUID NOT NULL REFERENCES project_repository_replacement_requests(id) ON DELETE CASCADE,
            voter_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            vote VARCHAR(8) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (request_id, voter_id)
        )
        """,
    ]

    for statement in ddl:
        db.execute(text(statement))
    db.commit()
    _TABLES_READY = True


def _get_project_by_slug(db: Session, slug: str) -> Mapping[str, object]:
    row = db.execute(select(projects).where(projects.c.slug == slug.lower())).mappings().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    from app.services.projects.helpers import _resolve_effective_project_subtype

    effective_subtype = _resolve_effective_project_subtype(db, row["id"], row["project_subtype"])
    if effective_subtype != "software":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Software governance requires a software project",
        )
    return row


def _get_membership(db: Session, project_id: UUID, user_id: UUID) -> Mapping[str, object] | None:
    return (
        db.execute(
            select(project_memberships).where(
                project_memberships.c.project_id == project_id,
                project_memberships.c.user_id == user_id,
            )
        )
        .mappings()
        .first()
    )


def _ensure_member(db: Session, project_id: UUID, user_id: UUID) -> None:
    if _get_membership(db, project_id, user_id) is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only project members can perform this action",
        )


def _is_merge_capable(db: Session, project_id: UUID, user_id: UUID) -> bool:
    row = db.execute(
        select(project_merge_capability_members.c.user_id).where(
            project_merge_capability_members.c.project_id == project_id,
            project_merge_capability_members.c.user_id == user_id,
        )
    ).first()
    return row is not None


def _username_map(db: Session, user_ids: set[UUID]) -> dict[UUID, str]:
    if not user_ids:
        return {}
    rows = db.execute(
        select(users.c.id, users.c.username).where(users.c.id.in_(list(user_ids)))
    ).all()
    return {row[0]: row[1] for row in rows}


def _detail_member_map(db: Session, user_ids: set[UUID]) -> dict[UUID, dict[str, object]]:
    if not user_ids:
        return {}
    rows = db.execute(
        select(users.c.id, users.c.username, users.c.bio).where(users.c.id.in_(list(user_ids)))
    ).all()
    return {
        row[0]: {
            "id": str(row[0]),
            "username": row[1],
            "bio": row[2] or "",
        }
        for row in rows
    }


def _vote_rows(db: Session, table_obj, request_id: UUID) -> list[Mapping[str, object]]:
    return (
        db.execute(select(table_obj).where(table_obj.c.request_id == request_id)).mappings().all()
    )


def _stage_label(stage: str) -> str:
    return PR_STAGE_LABELS.get(stage, stage.replace("-", " ").title())
