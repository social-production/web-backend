from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    comments,
    content_votes,
    conversation_members,
    event_memberships,
    events,
    help_requests,
    messages,
    posts,
    project_memberships,
    projects,
    threads,
    users,
)
from app.services.access_control import can_view_entity
from app.utils.votes import (
    weekly_active_event_members,
    weekly_active_project_members,
    weekly_active_users_global,
)

# Publicly visible surfaces should not fall below this engaged-audience floor when
# there is any activity beyond the author alone.
MIN_ENGAGED_PUBLIC_FLOOR = 3


def _count_followers(db: Session, author_id: UUID) -> int:
    from app.models import user_follows

    return int(
        db.execute(
            select(func.count())
            .select_from(user_follows)
            .where(user_follows.c.followed_id == author_id)
        ).scalar_one()
        or 0
    )


def _count_distinct_comment_authors(db: Session, *, subject_type: str, subject_id: UUID) -> int:
    return int(
        db.execute(
            select(func.count(func.distinct(comments.c.author_id)))
            .select_from(comments)
            .where(
                comments.c.subject_type == subject_type,
                comments.c.subject_id == subject_id,
                comments.c.author_id.is_not(None),
            )
        ).scalar_one()
        or 0
    )


def _count_distinct_content_voters(db: Session, *, target_id: UUID) -> int:
    return int(
        db.execute(
            select(func.count(func.distinct(content_votes.c.voter_id)))
            .select_from(content_votes)
            .where(content_votes.c.target_id == target_id)
        ).scalar_one()
        or 0
    )


def _hybrid_engaged_size(
    *,
    member_population: int,
    participant_count: int,
    voter_count: int = 0,
    public_surface: bool = True,
) -> int:
    """Combine formal membership with people who actually engaged the subject."""
    engaged = max(member_population, participant_count, voter_count)
    if public_surface and engaged > 1:
        engaged = max(engaged, MIN_ENGAGED_PUBLIC_FLOOR)
    return engaged


def _project_population(db: Session, project_id: UUID) -> int:
    weekly = weekly_active_project_members(db, project_id)
    members = weekly
    if members <= 0:
        members = int(
            db.execute(
                select(func.count())
                .select_from(project_memberships)
                .where(project_memberships.c.project_id == project_id)
            ).scalar_one()
            or 0
        )
    participants = _count_distinct_comment_authors(
        db, subject_type="project", subject_id=project_id
    )
    voters = _count_distinct_content_voters(db, target_id=project_id)
    return _hybrid_engaged_size(
        member_population=members,
        participant_count=participants,
        voter_count=voters,
        public_surface=True,
    )


def _event_population(db: Session, event_id: UUID) -> int:
    weekly = weekly_active_event_members(db, event_id)
    members = weekly
    if members <= 0:
        members = int(
            db.execute(
                select(func.count())
                .select_from(event_memberships)
                .where(event_memberships.c.event_id == event_id)
            ).scalar_one()
            or 0
        )
    participants = _count_distinct_comment_authors(db, subject_type="event", subject_id=event_id)
    voters = _count_distinct_content_voters(db, target_id=event_id)
    return _hybrid_engaged_size(
        member_population=members,
        participant_count=participants,
        voter_count=voters,
        public_surface=True,
    )


def _public_population(db: Session) -> int:
    weekly = weekly_active_users_global(db)
    if weekly > 0:
        return weekly
    return int(db.execute(select(func.count()).select_from(users)).scalar_one() or 0)


def electorate_size_for_target(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
    reported_author_id: UUID | None,
) -> int:
    normalized = target_type.strip().lower()

    if normalized == "project":
        size = _project_population(db, target_id)
    elif normalized == "event":
        size = _event_population(db, target_id)
    elif normalized == "post":
        row = (
            db.execute(select(posts.c.audience, posts.c.author_id).where(posts.c.id == target_id))
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")
        if str(row["audience"] or "public").lower() == "followers" and row["author_id"] is not None:
            size = _count_followers(db, row["author_id"])
        else:
            size = _public_population(db)
    elif normalized in {"thread", "help_request"}:
        participants = _count_distinct_comment_authors(
            db, subject_type=normalized, subject_id=target_id
        )
        voters = _count_distinct_content_voters(db, target_id=target_id)
        size = _hybrid_engaged_size(
            member_population=_public_population(db),
            participant_count=participants,
            voter_count=voters,
            public_surface=True,
        )
    elif normalized == "comment":
        row = (
            db.execute(
                select(comments.c.subject_type, comments.c.subject_id).where(
                    comments.c.id == target_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
        return electorate_size_for_target(
            db,
            target_type=str(row["subject_type"]),
            target_id=row["subject_id"],
            reported_author_id=reported_author_id,
        )
    elif normalized == "message":
        row = (
            db.execute(select(messages.c.conversation_id).where(messages.c.id == target_id))
            .mappings()
            .first()
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        size = int(
            db.execute(
                select(func.count())
                .select_from(conversation_members)
                .where(conversation_members.c.conversation_id == row["conversation_id"])
            ).scalar_one()
            or 0
        )
    else:
        size = _public_population(db)

    # Exclude the reported author from the eligible electorate when present.
    if reported_author_id is not None and size > 0:
        size = max(1, size - 1)
    return max(1, size)


def viewer_can_vote_on_target(
    db: Session,
    *,
    viewer_id: UUID,
    target_type: str,
    target_id: UUID,
    reported_author_id: UUID | None,
) -> bool:
    if reported_author_id is not None and viewer_id == reported_author_id:
        return False

    normalized = target_type.strip().lower()
    if normalized == "comment":
        row = (
            db.execute(
                select(comments.c.subject_type, comments.c.subject_id).where(
                    comments.c.id == target_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return False
        return can_view_entity(db, viewer_id, str(row["subject_type"]), row["subject_id"])

    if normalized == "message":
        row = (
            db.execute(select(messages.c.conversation_id).where(messages.c.id == target_id))
            .mappings()
            .first()
        )
        if row is None:
            return False
        member = db.execute(
            select(conversation_members.c.user_id).where(
                conversation_members.c.conversation_id == row["conversation_id"],
                conversation_members.c.user_id == viewer_id,
            )
        ).first()
        return member is not None

    if normalized in {"post", "thread", "project", "event", "help_request"}:
        return can_view_entity(db, viewer_id, normalized, target_id)

    return False


def _popularity_score(*, unique_voters: int, unique_commenters: int) -> int:
    """Popularity score from real participation.

    Weighting (views intentionally excluded until unique authenticated views exist):
    - unique upvoters / downvoters (content voters): +1 each
    - unique commenters / repliers: +2 each
    - members / followers / raw audience: used only for eligible electorate size
    - raw views: +0 for now
    """
    return max(0, int(unique_voters)) + max(0, int(unique_commenters)) * 2


def load_target_engagement(
    db: Session,
    *,
    target_type: str,
    target_id: UUID,
) -> tuple[object | None, int, int]:
    """Return (created_at, vote_count, popularity_score).

    ``vote_count`` remains the content's net vote tally for display callers.
    ``popularity_score`` drives age/popularity threshold boosts and is based on
    distinct participants, not membership size.
    """
    normalized = target_type.strip().lower()

    if normalized == "post":
        row = (
            db.execute(
                select(posts.c.created_at, posts.c.vote_count).where(posts.c.id == target_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None, 0, 0
        score = _popularity_score(
            unique_voters=_count_distinct_content_voters(db, target_id=target_id),
            unique_commenters=_count_distinct_comment_authors(
                db, subject_type="post", subject_id=target_id
            ),
        )
        return row["created_at"], int(row["vote_count"] or 0), score

    if normalized == "thread":
        row = (
            db.execute(
                select(threads.c.created_at, threads.c.vote_count).where(threads.c.id == target_id)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None, 0, 0
        score = _popularity_score(
            unique_voters=_count_distinct_content_voters(db, target_id=target_id),
            unique_commenters=_count_distinct_comment_authors(
                db, subject_type="thread", subject_id=target_id
            ),
        )
        return row["created_at"], int(row["vote_count"] or 0), score

    if normalized == "project":
        row = (
            db.execute(select(projects.c.created_at).where(projects.c.id == target_id))
            .mappings()
            .first()
        )
        if row is None:
            return None, 0, 0
        # Members shape eligible electorate size; popularity uses participants only.
        score = _popularity_score(
            unique_voters=_count_distinct_content_voters(db, target_id=target_id),
            unique_commenters=_count_distinct_comment_authors(
                db, subject_type="project", subject_id=target_id
            ),
        )
        return row["created_at"], 0, score

    if normalized == "event":
        row = (
            db.execute(select(events.c.created_at).where(events.c.id == target_id))
            .mappings()
            .first()
        )
        if row is None:
            return None, 0, 0
        score = _popularity_score(
            unique_voters=_count_distinct_content_voters(db, target_id=target_id),
            unique_commenters=_count_distinct_comment_authors(
                db, subject_type="event", subject_id=target_id
            ),
        )
        return row["created_at"], 0, score

    if normalized == "help_request":
        row = (
            db.execute(
                select(help_requests.c.created_at, help_requests.c.vote_count).where(
                    help_requests.c.id == target_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None, 0, 0
        score = _popularity_score(
            unique_voters=_count_distinct_content_voters(db, target_id=target_id),
            unique_commenters=_count_distinct_comment_authors(
                db, subject_type="help_request", subject_id=target_id
            ),
        )
        return row["created_at"], int(row["vote_count"] or 0), score

    if normalized == "comment":
        row = (
            db.execute(
                select(comments.c.created_at, comments.c.vote_count).where(
                    comments.c.id == target_id
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None, 0, 0
        reply_authors = int(
            db.execute(
                select(func.count(func.distinct(comments.c.author_id)))
                .select_from(comments)
                .where(
                    comments.c.parent_id == target_id,
                    comments.c.author_id.is_not(None),
                )
            ).scalar_one()
            or 0
        )
        score = _popularity_score(
            unique_voters=_count_distinct_content_voters(db, target_id=target_id),
            unique_commenters=reply_authors,
        )
        return row["created_at"], int(row["vote_count"] or 0), score

    if normalized == "message":
        row = (
            db.execute(select(messages.c.created_at).where(messages.c.id == target_id))
            .mappings()
            .first()
        )
        if row is None:
            return None, 0, 0
        # Chat messages have no separate popularity score yet (views excluded).
        return row["created_at"], 0, 0

    return None, 0, 0
