from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.models import (
    comment_attachments,
    comments,
    event_memberships,
    events,
    help_request_role_assignments,
    help_request_roles,
    help_requests,
    project_memberships,
    projects,
    subject_chat_reads,
    users,
)
from app.services.messages.util import _iso


def _comment_preview(db: Session, last_comment: object | None) -> str:
    if not last_comment:
        return ""
    body = str(last_comment["body"] or "").strip()
    if body:
        return body[:200]
    attachment = (
        db.execute(
            select(comment_attachments.c.kind, comment_attachments.c.filename).where(
                comment_attachments.c.comment_id == last_comment["id"]
            )
        )
        .mappings()
        .first()
    )
    if attachment is None:
        return ""
    if attachment["kind"] == "image":
        return "Photo"
    return str(attachment["filename"])[:200]


def _get_subject_chat_last_read_at(
    db: Session,
    current_user_id: UUID,
    subject_type: str,
    subject_id: UUID,
) -> datetime | None:
    row = db.execute(
        select(subject_chat_reads.c.last_read_at).where(
            subject_chat_reads.c.user_id == current_user_id,
            subject_chat_reads.c.subject_type == subject_type,
            subject_chat_reads.c.subject_id == subject_id,
        )
    ).first()
    return row[0] if row else None


def _linked_chat_unread_count(
    db: Session,
    subject_type: str,
    subject_id: UUID,
    current_user_id: UUID,
    last_read_at: datetime | None,
) -> int:
    conditions = [
        comments.c.subject_type == subject_type,
        comments.c.subject_id == subject_id,
        comments.c.author_id != current_user_id,
    ]
    if last_read_at is not None:
        conditions.append(comments.c.created_at > last_read_at)

    count = db.execute(select(func.count()).select_from(comments).where(*conditions)).scalar_one()
    return int(count or 0)


def get_linked_chats(db: Session, current_user_id: UUID) -> dict[str, object]:
    event_ids = {
        row[0]
        for row in db.execute(
            select(event_memberships.c.event_id).where(
                event_memberships.c.user_id == current_user_id
            )
        ).all()
    }
    project_ids = {
        row[0]
        for row in db.execute(
            select(project_memberships.c.project_id).where(
                project_memberships.c.user_id == current_user_id
            )
        ).all()
    }

    for subject_type, target_ids in (("event", event_ids), ("project", project_ids)):
        commented_ids = (
            db.execute(
                select(comments.c.subject_id)
                .where(
                    comments.c.subject_type == subject_type,
                    comments.c.author_id == current_user_id,
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        target_ids.update(commented_ids)

    items = []

    if event_ids:
        event_rows = (
            db.execute(
                select(
                    events.c.id,
                    events.c.slug,
                    events.c.title,
                    events.c.last_activity_at,
                    events.c.comment_count,
                )
                .where(events.c.id.in_(list(event_ids)))
                .order_by(events.c.last_activity_at.desc())
            )
            .mappings()
            .all()
        )

        for row in event_rows:
            last_comment = (
                db.execute(
                    select(comments.c.id, comments.c.body, comments.c.created_at)
                    .where(
                        comments.c.subject_type == "event",
                        comments.c.subject_id == row["id"],
                    )
                    .order_by(comments.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            last_read_at = _get_subject_chat_last_read_at(db, current_user_id, "event", row["id"])
            unread_count = _linked_chat_unread_count(
                db,
                "event",
                row["id"],
                current_user_id,
                last_read_at,
            )

            items.append(
                {
                    "id": str(row["id"]),
                    "kind": "event",
                    "entity_id": str(row["id"]),
                    "entity_slug": row["slug"],
                    "title": row["title"],
                    "preview": _comment_preview(db, last_comment),
                    "last_message_at": _iso(last_comment["created_at"])
                    if last_comment
                    else _iso(row["last_activity_at"]),
                    "comment_count": row["comment_count"],
                    "unread_count": unread_count,
                }
            )

    if project_ids:
        project_rows = (
            db.execute(
                select(
                    projects.c.id,
                    projects.c.slug,
                    projects.c.title,
                    projects.c.last_activity_at,
                    projects.c.comment_count,
                )
                .where(projects.c.id.in_(list(project_ids)))
                .order_by(projects.c.last_activity_at.desc())
            )
            .mappings()
            .all()
        )

        for row in project_rows:
            last_comment = (
                db.execute(
                    select(comments.c.id, comments.c.body, comments.c.created_at)
                    .where(
                        comments.c.subject_type == "project",
                        comments.c.subject_id == row["id"],
                    )
                    .order_by(comments.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            last_read_at = _get_subject_chat_last_read_at(db, current_user_id, "project", row["id"])
            unread_count = _linked_chat_unread_count(
                db,
                "project",
                row["id"],
                current_user_id,
                last_read_at,
            )
            member_names = [
                username
                for (username,) in db.execute(
                    select(users.c.username)
                    .select_from(
                        project_memberships.join(users, users.c.id == project_memberships.c.user_id)
                    )
                    .where(project_memberships.c.project_id == row["id"])
                    .order_by(users.c.username.asc())
                ).all()
            ]

            items.append(
                {
                    "id": str(row["id"]),
                    "kind": "project",
                    "entity_id": str(row["id"]),
                    "entity_slug": row["slug"],
                    "title": row["title"],
                    "preview": _comment_preview(db, last_comment),
                    "last_message_at": _iso(last_comment["created_at"])
                    if last_comment
                    else _iso(row["last_activity_at"]),
                    "comment_count": row["comment_count"],
                    "member_count": len(member_names),
                    "members": member_names,
                    "unread_count": unread_count,
                }
            )

    help_request_ids: set[UUID] = set()
    help_request_ids.update(
        db.execute(select(help_requests.c.id).where(help_requests.c.author_id == current_user_id))
        .scalars()
        .all()
    )
    help_request_ids.update(
        db.execute(
            select(help_request_roles.c.help_request_id)
            .select_from(
                help_request_role_assignments.join(
                    help_request_roles,
                    help_request_roles.c.id == help_request_role_assignments.c.role_id,
                )
            )
            .where(help_request_role_assignments.c.user_id == current_user_id)
            .distinct()
        )
        .scalars()
        .all()
    )
    help_request_ids.update(
        db.execute(
            select(comments.c.subject_id)
            .where(
                comments.c.subject_type == "help_request",
                comments.c.author_id == current_user_id,
            )
            .distinct()
        )
        .scalars()
        .all()
    )

    if help_request_ids:
        help_request_rows = (
            db.execute(
                select(
                    help_requests.c.id,
                    help_requests.c.title,
                    help_requests.c.comment_count,
                    help_requests.c.created_at,
                ).where(help_requests.c.id.in_(list(help_request_ids)))
            )
            .mappings()
            .all()
        )

        for row in help_request_rows:
            last_comment = (
                db.execute(
                    select(comments.c.id, comments.c.body, comments.c.created_at)
                    .where(
                        comments.c.subject_type == "help_request",
                        comments.c.subject_id == row["id"],
                    )
                    .order_by(comments.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
            last_read_at = _get_subject_chat_last_read_at(
                db, current_user_id, "help_request", row["id"]
            )
            unread_count = _linked_chat_unread_count(
                db,
                "help_request",
                row["id"],
                current_user_id,
                last_read_at,
            )

            items.append(
                {
                    "id": str(row["id"]),
                    "kind": "help_request",
                    "entity_id": str(row["id"]),
                    "entity_slug": str(row["id"]),
                    "title": row["title"],
                    "preview": _comment_preview(db, last_comment),
                    "last_message_at": _iso(last_comment["created_at"])
                    if last_comment
                    else _iso(row["created_at"]),
                    "comment_count": row["comment_count"],
                    "unread_count": unread_count,
                }
            )

    pref_rows = (
        db.execute(
            select(subject_chat_reads).where(subject_chat_reads.c.user_id == current_user_id)
        )
        .mappings()
        .all()
    )
    prefs = {(row["subject_type"], str(row["subject_id"])): row for row in pref_rows}
    visible = []
    for item in items:
        pref = prefs.get((item["kind"], item["entity_id"]))
        if pref is not None and pref["hidden_at"] is not None:
            continue
        item["list_pinned"] = pref is not None and pref["list_pinned_at"] is not None
        item["muted"] = pref is not None and pref["muted_at"] is not None
        visible.append(item)
    visible.sort(key=lambda item: item["last_message_at"], reverse=True)
    visible.sort(key=lambda item: 0 if item["list_pinned"] else 1)
    return {"total": len(visible), "items": visible}


def set_linked_chat_preferences(
    db: Session,
    current_user_id: UUID,
    subject_type: str,
    subject_id: UUID,
    *,
    pinned: bool | None = None,
    muted: bool | None = None,
    hidden: bool | None = None,
) -> dict[str, object]:
    normalized_subject_type = subject_type.strip().lower()
    if normalized_subject_type not in {"project", "event", "help_request"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subject_type must be project, event, or help_request",
        )

    now = datetime.now(UTC)
    existing = db.execute(
        select(subject_chat_reads.c.user_id).where(
            subject_chat_reads.c.user_id == current_user_id,
            subject_chat_reads.c.subject_type == normalized_subject_type,
            subject_chat_reads.c.subject_id == subject_id,
        )
    ).first()
    values: dict[str, object] = {}
    if pinned is not None:
        values["list_pinned_at"] = now if pinned else None
    if muted is not None:
        values["muted_at"] = now if muted else None
    if hidden is not None:
        values["hidden_at"] = now if hidden else None
    if existing is None:
        db.execute(
            insert(subject_chat_reads).values(
                user_id=current_user_id,
                subject_type=normalized_subject_type,
                subject_id=subject_id,
                last_read_at=now,
                **values,
            )
        )
    elif values:
        db.execute(
            update(subject_chat_reads)
            .where(
                subject_chat_reads.c.user_id == current_user_id,
                subject_chat_reads.c.subject_type == normalized_subject_type,
                subject_chat_reads.c.subject_id == subject_id,
            )
            .values(**values)
        )
    db.commit()
    return {"ok": True}


def mark_linked_chat_read(
    db: Session,
    current_user_id: UUID,
    subject_type: str,
    subject_id: UUID,
) -> dict[str, object]:
    normalized_subject_type = subject_type.strip().lower()
    if normalized_subject_type not in {"project", "event", "help_request"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="subject_type must be project, event, or help_request",
        )

    if normalized_subject_type == "project":
        exists = db.execute(select(projects.c.id).where(projects.c.id == subject_id)).first()
    elif normalized_subject_type == "event":
        exists = db.execute(select(events.c.id).where(events.c.id == subject_id)).first()
    else:
        exists = db.execute(
            select(help_requests.c.id).where(help_requests.c.id == subject_id)
        ).first()

    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{normalized_subject_type.capitalize()} not found",
        )

    now = datetime.now(UTC)
    existing = db.execute(
        select(subject_chat_reads.c.user_id).where(
            subject_chat_reads.c.user_id == current_user_id,
            subject_chat_reads.c.subject_type == normalized_subject_type,
            subject_chat_reads.c.subject_id == subject_id,
        )
    ).first()

    if existing is None:
        db.execute(
            insert(subject_chat_reads).values(
                user_id=current_user_id,
                subject_type=normalized_subject_type,
                subject_id=subject_id,
                last_read_at=now,
            )
        )
    else:
        db.execute(
            update(subject_chat_reads)
            .where(
                subject_chat_reads.c.user_id == current_user_id,
                subject_chat_reads.c.subject_type == normalized_subject_type,
                subject_chat_reads.c.subject_id == subject_id,
            )
            .values(last_read_at=now)
        )

    db.commit()
    return {
        "ok": True,
        "subject_type": normalized_subject_type,
        "subject_id": subject_id,
        "last_read_at": now,
    }
