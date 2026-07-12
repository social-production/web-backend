from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import insert, select

from app.db import SessionLocal
from app.models import channels, notifications, posts, scope_memberships, threads, users
from app.routers.content import DiscussionCommentOut
from app.services.content import create_thread, get_thread_by_slug
from app.services.feeds import get_user_feed
from app.services.governance import add_comment, get_comments


def test_discussion_comment_out_preserves_replies() -> None:
    nested = DiscussionCommentOut(
        id=uuid4(),
        author_username="alice",
        body="reply",
        vote_count=0,
        active_vote=0,
        created_at=datetime.now(timezone.utc),
    )
    root = DiscussionCommentOut(
        id=uuid4(),
        author_username="bob",
        body="root",
        vote_count=0,
        active_vote=0,
        created_at=datetime.now(timezone.utc),
        replies=[nested],
    )

    payload = root.model_dump()
    assert len(payload["replies"]) == 1
    assert payload["replies"][0]["body"] == "reply"


def test_thread_nested_reply_is_returned_and_notifies_parent_author() -> None:
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    author_id = uuid4()
    replier_id = uuid4()
    slug = f"nested-thread-{uuid4()}"

    db.execute(
        insert(users).values(
            id=author_id,
            username=f"author-{str(author_id)[:8]}",
            email=f"author-{author_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=replier_id,
            username=f"replier-{str(replier_id)[:8]}",
            email=f"replier-{replier_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    channel_id = uuid4()
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=f"ch-{str(channel_id)[:8]}",
            name="Test channel",
            description="test",
            created_by=author_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="channel",
            scope_id=channel_id,
            user_id=author_id,
            role="member",
            created_at=now,
        )
    )
    db.commit()

    create_thread(
        db=db,
        current_user_id=author_id,
        slug=slug,
        title="Nested reply thread",
        body="Root thread body",
        channel_slugs=[f"ch-{str(channel_id)[:8]}"],
        community_slugs=[],
    )

    thread_row = db.execute(select(threads.c.id).where(threads.c.slug == slug)).first()
    assert thread_row is not None
    thread_id = thread_row[0]

    parent = add_comment(
        db=db,
        current_user_id=author_id,
        subject_type="thread",
        subject_id=thread_id,
        body="Parent comment",
    )
    parent_id = parent["comment"]["id"]

    reply = add_comment(
        db=db,
        current_user_id=replier_id,
        subject_type="thread",
        subject_id=thread_id,
        body="Nested reply",
        parent_id=parent_id,
    )
    reply_id = reply["comment"]["id"]

    discussion = get_comments(db, "thread", thread_id, author_id)["items"]
    assert len(discussion) == 1
    assert len(discussion[0]["replies"]) == 1
    assert discussion[0]["replies"][0]["body"] == "Nested reply"

    thread_payload = get_thread_by_slug(db, slug, author_id)
    thread_discussion = thread_payload["thread"]["discussion"]
    assert len(thread_discussion) == 1
    assert len(thread_discussion[0]["replies"]) == 1

    owner_notifications = db.execute(
        select(notifications.c.kind, notifications.c.href).where(
            notifications.c.recipient_id == author_id,
            notifications.c.target_id == reply_id,
        )
    ).all()
    assert owner_notifications
    assert owner_notifications[0][0] == "reply"
    assert f"?comment={reply_id}" in owner_notifications[0][1]

    db.close()


def test_post_comment_notifies_post_author() -> None:
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    author_id = uuid4()
    commenter_id = uuid4()
    post_id = uuid4()

    db.execute(
        insert(users).values(
            id=author_id,
            username=f"post-author-{str(author_id)[:8]}",
            email=f"post-author-{author_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=commenter_id,
            username=f"commenter-{str(commenter_id)[:8]}",
            email=f"commenter-{commenter_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(posts).values(
            id=post_id,
            author_id=author_id,
            body="Author post body",
            audience="public",
            vote_count=0,
            comment_count=0,
            created_at=now,
            updated_at=now,
        )
    )
    db.commit()

    created = add_comment(
        db=db,
        current_user_id=commenter_id,
        subject_type="post",
        subject_id=post_id,
        body="A comment on the post",
    )
    comment_id = created["comment"]["id"]

    rows = db.execute(
        select(notifications.c.kind, notifications.c.href).where(
            notifications.c.recipient_id == author_id,
            notifications.c.target_id == comment_id,
        )
    ).all()
    assert rows
    assert rows[0][0] == "reply"
    assert rows[0][1] == f"/posts/{post_id}?comment={comment_id}"

    db.close()


def test_user_feed_includes_comment_activity() -> None:
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    author_id = uuid4()
    commenter_id = uuid4()
    slug = f"profile-feed-thread-{uuid4()}"
    commenter_username = f"commenter-{str(commenter_id)[:8]}"

    db.execute(
        insert(users).values(
            id=author_id,
            username=f"author-{str(author_id)[:8]}",
            email=f"author-{author_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=commenter_id,
            username=commenter_username,
            email=f"commenter-{commenter_id}@test.invalid",
            password_hash="x",
            created_at=now,
            updated_at=now,
        )
    )
    channel_id = uuid4()
    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=f"ch-{str(channel_id)[:8]}",
            name="Profile feed channel",
            description="test",
            created_by=author_id,
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(scope_memberships).values(
            id=uuid4(),
            scope_kind="channel",
            scope_id=channel_id,
            user_id=author_id,
            role="member",
            created_at=now,
        )
    )
    db.commit()

    create_thread(
        db=db,
        current_user_id=author_id,
        slug=slug,
        title="Profile feed thread",
        body="Thread body",
        channel_slugs=[f"ch-{str(channel_id)[:8]}"],
        community_slugs=[],
    )

    thread_row = db.execute(select(threads.c.id).where(threads.c.slug == slug)).first()
    assert thread_row is not None
    thread_id = thread_row[0]

    created = add_comment(
        db=db,
        current_user_id=commenter_id,
        subject_type="thread",
        subject_id=thread_id,
        body="Visible profile comment",
    )
    comment_id = str(created["comment"]["id"])

    feed = get_user_feed(db, commenter_username, viewer_user_id=author_id)
    comment_items = [item for item in feed["items"] if item["entity_type"] == "comment_activity"]

    assert comment_items
    assert comment_items[0]["id"] == comment_id
    assert comment_items[0]["title"] == "Profile feed thread"
    assert comment_items[0]["body"] == "Visible profile comment"
    assert comment_items[0]["feed_source"] == "activity"

    db.close()
