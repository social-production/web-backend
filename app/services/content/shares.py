from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import users
from app.services.content.help_requests import get_help_request_by_id
from app.services.content.posts import get_post_by_id
from app.services.content.threads import get_thread_by_slug
from app.services.notifications import create_notification
from app.utils.usernames import username_matches


def _target_user(db: Session, current_user_id: UUID, username: str):
    normalized = username.strip()
    if not normalized:
        return None, {"ok": False, "error": "Choose another user."}

    target = (
        db.execute(select(users.c.id, users.c.username).where(username_matches(normalized)))
        .mappings()
        .first()
    )
    if target is None or target["id"] == current_user_id:
        return None, {"ok": False, "error": "Choose another user."}
    return target, None


def share_post_with_user(
    db: Session,
    current_user_id: UUID,
    post_id: UUID,
    username: str,
) -> dict[str, object]:
    target, error = _target_user(db, current_user_id, username)
    if error or target is None:
        return error or {"ok": False, "error": "Choose another user."}

    post = get_post_by_id(db, post_id, current_user_id)["post"]
    body = str(post.get("body") or "").strip()
    title = body.split("\n", 1)[0].strip()[:80] or "Post"
    create_notification(
        db=db,
        recipient_id=target["id"],
        actor_id=current_user_id,
        kind="post-share",
        surface="post",
        subject_type="post",
        subject_id=post["id"],
        target_id=post["id"],
        title=title,
        body="A post was shared with you.",
        href=f"/posts/{post['id']}",
    )
    return {"ok": True}


def share_thread_with_user(
    db: Session,
    current_user_id: UUID,
    slug: str,
    username: str,
) -> dict[str, object]:
    target, error = _target_user(db, current_user_id, username)
    if error or target is None:
        return error or {"ok": False, "error": "Choose another user."}

    thread = get_thread_by_slug(db, slug, current_user_id)["thread"]
    create_notification(
        db=db,
        recipient_id=target["id"],
        actor_id=current_user_id,
        kind="thread-share",
        surface="thread",
        subject_type="thread",
        subject_id=thread["id"],
        target_id=thread["id"],
        title=str(thread.get("title") or "Thread"),
        body="A thread was shared with you.",
        href=f"/threads/{thread['slug']}",
    )
    return {"ok": True}


def share_help_request_with_user(
    db: Session,
    current_user_id: UUID,
    help_request_id: UUID,
    username: str,
) -> dict[str, object]:
    target, error = _target_user(db, current_user_id, username)
    if error or target is None:
        return error or {"ok": False, "error": "Choose another user."}

    payload = get_help_request_by_id(db, help_request_id, current_user_id)
    help_request = payload["help_request"]
    create_notification(
        db=db,
        recipient_id=target["id"],
        actor_id=current_user_id,
        kind="hr-share",
        surface="help-request",
        subject_type="help-request",
        subject_id=help_request["id"],
        target_id=help_request["id"],
        title=str(help_request.get("title") or "Help request"),
        body="A help request was shared with you.",
        href=f"/help-requests/{help_request['id']}",
    )
    return {"ok": True}
