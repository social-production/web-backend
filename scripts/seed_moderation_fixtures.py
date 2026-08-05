from __future__ import annotations

"""Additive fixtures for end-to-end moderation testing across core surfaces.

Creates fresh event/project/thread/post/help-request content plus comments and a
DM, authored by victoria-demo for reporting by reporter-demo.

Usage (from web-backend):
  PYTHONPATH=. python scripts/seed_moderation_fixtures.py
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.auth.passwords import hash_password
from app.config import get_settings
from app.models import channels, locations, users
from app.services.content.help_requests import create_help_request
from app.services.content.posts import create_post
from app.services.content.threads import create_thread
from app.services.events.helpers import create_event
from app.services.governance import add_comment
from app.services.messages.conversations import start_direct_conversation
from app.services.messages.messaging import send_message
from app.services.projects.helpers import create_project

AUTHOR = "victoria-demo"
AUTHOR_PASSWORD = "victoria-demo-dev"
REPORTER = "reporter-demo"
REPORTER_PASSWORD = "reporter-demo-dev"
RECIPIENT = "abc"
CHANNEL = "victoria"


def ensure_user(db: Session, username: str, password: str) -> UUID:
    row = db.execute(select(users.c.id).where(users.c.username == username)).scalar_one_or_none()
    if row is not None:
        return row
    inserted = db.execute(
        users.insert()
        .values(
            username=username,
            password_hash=hash_password(password),
            display_name=username,
        )
        .returning(users.c.id)
    ).scalar_one()
    db.commit()
    return inserted


def main() -> None:
    tag = f"moderation-qa-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)

    with Session(engine) as db:
        author_id = ensure_user(db, AUTHOR, AUTHOR_PASSWORD)
        ensure_user(db, REPORTER, REPORTER_PASSWORD)
        recipient_id = db.execute(
            select(users.c.id).where(users.c.username == RECIPIENT)
        ).scalar_one_or_none()
        if recipient_id is None:
            raise SystemExit(f"missing recipient user {RECIPIENT}")
        channel = db.execute(
            select(channels.c.slug).where(channels.c.slug == CHANNEL)
        ).scalar_one_or_none()
        if channel is None:
            raise SystemExit(f"missing channel {CHANNEL}")

        loc = (
            db.execute(
                select(locations.c.id, locations.c.display_label).where(
                    locations.c.provider_place_id == "seed:victoria:collingwood"
                )
            )
            .mappings()
            .first()
        )
        location_id = loc["id"] if loc else None
        location_label = (
            loc["display_label"] if loc else "Collingwood Town Hall, Collingwood VIC"
        )

        project = create_project(
            db,
            author_id,
            title=f"{tag} community garden",
            description="Fresh moderation fixture project. Please use this for report testing.",
            project_mode="productive",
            project_subtype="standard",
            location_label=location_label,
            channel_slugs=[CHANNEL],
            location_id=location_id,
        )["project"]

        event = create_event(
            db,
            author_id,
            title=f"{tag} neighborhood meetup",
            description="Fresh moderation fixture event for report testing.",
            is_private=False,
            time_label="Next weekend",
            location_label=location_label,
            channel_slugs=[CHANNEL],
            scheduled_at=datetime.now(UTC) + timedelta(days=7),
            audience="public",
            location_id=location_id,
        )["event"]

        thread = create_thread(
            db,
            author_id,
            title=f"{tag} cleanup discussion",
            body="Fresh moderation fixture thread. Report this thread and its comments.",
            channel_slugs=[CHANNEL],
        )["thread"]

        post = create_post(
            db,
            author_id,
            body=f"{tag} public update — fixture post for moderation reporting.",
            audience="public",
        )["post"]

        help_request = create_help_request(
            db,
            author_id,
            title=f"{tag} need hands for tidy-up",
            body="Fresh moderation fixture help request.",
            location_label=location_label,
            needed_at=datetime.now(UTC) + timedelta(days=3),
            roles=[{"title": "Helper", "description": "General help", "slots": 3}],
            channel_slugs=[CHANNEL],
            location_id=location_id,
        )["help_request"]

        thread_comment = add_comment(
            db,
            author_id,
            "thread",
            UUID(str(thread["id"])),
            "Thread comment fixture — please report me.",
        )["comment"]
        post_comment = add_comment(
            db,
            author_id,
            "post",
            UUID(str(post["id"])),
            "Post comment fixture — please report me.",
        )["comment"]
        project_comment = add_comment(
            db,
            author_id,
            "project",
            UUID(str(project["id"])),
            "Project discussion comment fixture — please report me.",
        )["comment"]
        event_comment = add_comment(
            db,
            author_id,
            "event",
            UUID(str(event["id"])),
            "Event chat comment fixture — please report me.",
        )["comment"]
        help_comment = add_comment(
            db,
            author_id,
            "help_request",
            UUID(str(help_request["id"])),
            "Help-request comment fixture — please report me.",
        )["comment"]

        conversation = start_direct_conversation(db, author_id, RECIPIENT)["conversation"]
        message = send_message(
            db,
            author_id,
            UUID(str(conversation["id"])),
            f"{tag} DM fixture message — please report me from chat.",
        )["message"]

        print("=== Moderation fixtures seeded ===")
        print(f"tag: {tag}")
        print(f"author: {AUTHOR} / {AUTHOR_PASSWORD}")
        print(f"reporter: {REPORTER} / {REPORTER_PASSWORD}")
        print(f"recipient: {RECIPIENT}")
        print(f"project: /projects/{project['slug']}  id={project['id']}")
        print(f"event: /events/{event['slug']}  id={event['id']}")
        print(f"thread: /threads/{thread['slug']}  id={thread['id']}")
        print(f"post: /posts/{post['id']}")
        print(f"help_request: /help-requests/{help_request['id']}")
        print(f"thread_comment_id: {thread_comment['id']}")
        print(f"post_comment_id: {post_comment['id']}")
        print(f"project_comment_id: {project_comment['id']}")
        print(f"event_comment_id: {event_comment['id']}")
        print(f"help_comment_id: {help_comment['id']}")
        print(f"conversation_id: {conversation['id']}")
        print(f"message_id: {message['id']}")
        print(f"messages: /messages?conversation={conversation['id']}")


if __name__ == "__main__":
    main()
