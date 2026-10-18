"""Public bot reviews.

Bot marks never pause anyone on their own. Three marks from full members open a review that
every full member can vote on (platform quorum, 66% "bot" share). Upheld: the account is paused
(read-only, votes count for nothing) and anyone who vouched for it can't vouch for 30 days.
Cleared, or not decided within a week: the marks are removed and those markers can't mark the
account again for 30 days. A paused account can ask for one re-review 30 days later.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import (
    account_stances,
    bot_mark_cooldowns,
    bot_review_votes,
    bot_reviews,
    conversation_members,
    event_memberships,
    project_memberships,
    scope_memberships,
    users,
)
from app.services.standing import FULL, compute_standings
from app.utils.votes import required_votes, weekly_active_users_global

MARKS_TO_OPEN = 3
REVIEW_DAYS = 7
COOLDOWN_DAYS = 30
APPEAL_AFTER_DAYS = 30
BOT_SHARE = 0.66
MIN_VOTES = 3
# Most people told about one review, so a busy account can't flood everyone's bell.
MAX_REVIEW_NOTICES = 100


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _open_review(db: Session, target_id: UUID):
    return (
        db.execute(
            select(bot_reviews).where(
                bot_reviews.c.target_user_id == target_id, bot_reviews.c.status == "open"
            )
        )
        .mappings()
        .first()
    )


def _latest_closed(db: Session, target_id: UUID):
    return (
        db.execute(
            select(bot_reviews)
            .where(
                bot_reviews.c.target_user_id == target_id,
                bot_reviews.c.status.in_(("upheld", "cleared")),
            )
            .order_by(bot_reviews.c.closed_at.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )


def _cooling_markers(db: Session, target_id: UUID) -> set:
    return set(
        db.execute(
            select(bot_mark_cooldowns.c.marker_id).where(
                bot_mark_cooldowns.c.target_user_id == target_id,
                bot_mark_cooldowns.c.until > _now(),
            )
        ).scalars()
    )


def marker_on_cooldown(db: Session, marker_id: UUID, target_id: UUID) -> bool:
    return marker_id in _cooling_markers(db, target_id)


def vouch_banned_until(db: Session, user_id: UUID) -> datetime | None:
    """Vouching pauses for 30 days after someone you vouched for is found to be a bot."""
    vouched = select(account_stances.c.target_user_id).where(
        account_stances.c.source_user_id == user_id, account_stances.c.stance == "vouch"
    )
    closed = db.execute(
        select(bot_reviews.c.closed_at).where(
            bot_reviews.c.target_user_id.in_(vouched),
            bot_reviews.c.status == "upheld",
            bot_reviews.c.closed_at > _now() - timedelta(days=COOLDOWN_DAYS),
        )
    ).scalars()
    latest = max((_aware(value) for value in closed if value is not None), default=None)
    return latest + timedelta(days=COOLDOWN_DAYS) if latest else None


def _counting_markers(db: Session, target_id: UUID, standings: dict) -> list:
    cooling = _cooling_markers(db, target_id)
    markers = db.execute(
        select(account_stances.c.source_user_id).where(
            account_stances.c.target_user_id == target_id, account_stances.c.stance == "bot"
        )
    ).scalars()
    return [
        marker
        for marker in markers
        if marker not in cooling and getattr(standings.get(marker), "level", None) == FULL
    ]


def _notify(db: Session, target_id: UUID, review_id: UUID, title: str, body: str) -> None:
    from app.services.notifications import create_notification

    username = db.execute(select(users.c.username).where(users.c.id == target_id)).scalar_one()
    create_notification(
        db,
        recipient_id=target_id,
        kind="bot-review",
        surface="account",
        subject_type="account",
        subject_id=review_id,
        title=title,
        body=body,
        href=f"/profile/{username}",
    )


def maybe_open_review(db: Session, target_id: UUID) -> None:
    """Called after a bot mark. Opens a review once three full members' marks count."""
    if _open_review(db, target_id) is not None:
        return
    standings = compute_standings(db)
    if getattr(standings.get(target_id), "level", None) == "paused":
        return
    if len(_counting_markers(db, target_id, standings)) < MARKS_TO_OPEN:
        return
    review_id = uuid4()
    db.execute(insert(bot_reviews).values(id=review_id, target_user_id=target_id))
    _notify(
        db,
        target_id,
        review_id,
        "Members asked for a bot review of your account",
        "Full members are voting on whether this account belongs to a real person. "
        "You can keep posting while the review is open.",
    )
    _notify_people_who_know_them(db, target_id, review_id, standings)


def _shared_member_ids(db: Session, target_id: UUID) -> set:
    """Everyone who shares a community, channel, project, event or chat with the account."""
    shared: set = set()
    for table, key in (
        (scope_memberships, (scope_memberships.c.scope_kind, scope_memberships.c.scope_id)),
        (project_memberships, (project_memberships.c.project_id,)),
        (event_memberships, (event_memberships.c.event_id,)),
        (conversation_members, (conversation_members.c.conversation_id,)),
    ):
        theirs = select(*key).where(table.c.user_id == target_id)
        rows = db.execute(
            select(table.c.user_id)
            .where(tuple_(*key).in_(theirs) if len(key) > 1 else key[0].in_(theirs))
            .distinct()
        ).scalars()
        shared.update(rows)
    shared.discard(target_id)
    return shared


def _notify_people_who_know_them(
    db: Session, target_id: UUID, review_id: UUID, standings: dict
) -> None:
    """Full members who share a space with the account hear about the review and can vote.

    The people who marked it already know; nobody else is told who marked it.
    """
    from app.services.notifications import create_notification

    markers = set(
        db.execute(
            select(account_stances.c.source_user_id).where(
                account_stances.c.target_user_id == target_id, account_stances.c.stance == "bot"
            )
        ).scalars()
    )
    voters = [
        user_id
        for user_id in _shared_member_ids(db, target_id)
        if user_id not in markers and getattr(standings.get(user_id), "level", None) == FULL
    ][:MAX_REVIEW_NOTICES]
    username = db.execute(select(users.c.username).where(users.c.id == target_id)).scalar_one()
    for voter_id in voters:
        create_notification(
            db,
            recipient_id=voter_id,
            kind="bot-review-vote",
            surface="account",
            subject_type="account",
            subject_id=review_id,
            target_id=target_id,
            title=f"Is {username} a real person?",
            body=(
                f"{username} is in a bot review, and you share a space with them. "
                "If you know them, vote Real person or Bot on their profile."
            ),
            href=f"/profile/{username}",
        )


def _tally(db: Session, review, standings: dict) -> dict:
    rows = db.execute(
        select(bot_review_votes.c.voter_id, bot_review_votes.c.vote).where(
            bot_review_votes.c.review_id == review["id"]
        )
    ).all()
    counted = [
        (voter, vote)
        for voter, vote in rows
        if getattr(standings.get(voter), "level", None) == FULL
    ]
    bot_votes = sum(1 for _voter, vote in counted if vote == "bot")
    real_votes = sum(1 for _voter, vote in counted if vote == "real")
    population = weekly_active_users_global(db)
    # Never fewer than three votes, however quiet the week, so one or two people can't decide.
    needed = max(MIN_VOTES, required_votes(population))
    total = bot_votes + real_votes
    remaining = max(0, max(population, needed) - total)
    passing = total >= needed and bot_votes / total >= BOT_SHARE if total else False
    max_total = total + remaining
    can_still_pass = (
        not passing and max_total >= needed and (bot_votes + remaining) / max_total >= BOT_SHARE
        if max_total
        else False
    )
    return {
        "bot_votes": bot_votes,
        "real_votes": real_votes,
        "votes_required": needed,
        "passing": passing,
        "can_still_pass": can_still_pass,
    }


def _close(db: Session, review, outcome: str) -> None:
    now = _now()
    db.execute(
        update(bot_reviews)
        .where(bot_reviews.c.id == review["id"])
        .values(status=outcome, closed_at=now)
    )
    target_id = review["target_user_id"]
    if outcome == "upheld":
        _notify(
            db,
            target_id,
            review["id"],
            "A review found this account is a bot",
            "The account is now read-only. You can ask for one re-review in 30 days.",
        )
        return
    markers = list(
        db.execute(
            select(account_stances.c.source_user_id).where(
                account_stances.c.target_user_id == target_id, account_stances.c.stance == "bot"
            )
        ).scalars()
    )
    db.execute(
        delete(account_stances).where(
            account_stances.c.target_user_id == target_id, account_stances.c.stance == "bot"
        )
    )
    for marker in markers:
        until = now + timedelta(days=COOLDOWN_DAYS)
        db.execute(
            pg_insert(bot_mark_cooldowns)
            .values(marker_id=marker, target_user_id=target_id, until=until)
            .on_conflict_do_update(
                index_elements=[
                    bot_mark_cooldowns.c.marker_id,
                    bot_mark_cooldowns.c.target_user_id,
                ],
                set_={"until": until},
            )
        )
    _notify(
        db,
        target_id,
        review["id"],
        "The bot review cleared your account",
        "Members agreed this account belongs to a real person.",
    )


def settle(db: Session, review, standings: dict | None = None) -> str:
    """Close the review if it is decided or has run out of time. Returns the status."""
    if review is None or review["status"] != "open":
        return review["status"] if review is not None else "none"
    standings = standings or compute_standings(db)
    tally = _tally(db, review, standings)
    expired = _aware(review["opened_at"]) < _now() - timedelta(days=REVIEW_DAYS)
    if tally["passing"]:
        _close(db, review, "upheld")
        return "upheld"
    # Close early only once "real person" votes make a bot verdict impossible.
    if expired or (tally["real_votes"] > 0 and not tally["can_still_pass"]):
        _close(db, review, "cleared")
        return "cleared"
    return "open"


def cast_vote(db: Session, voter_id: UUID, target_id: UUID, vote: str) -> None:
    if vote not in ("bot", "real"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="vote must be bot or real"
        )
    if voter_id == target_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can't vote on your own review"
        )
    standings = compute_standings(db)
    if getattr(standings.get(voter_id), "level", None) != FULL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bot reviews are decided by full members.",
        )
    review = _open_review(db, target_id)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No open review")
    db.execute(
        pg_insert(bot_review_votes)
        .values(review_id=review["id"], voter_id=voter_id, vote=vote)
        .on_conflict_do_update(
            index_elements=[bot_review_votes.c.review_id, bot_review_votes.c.voter_id],
            set_={"vote": vote},
        )
    )
    settle(db, review, standings)
    db.commit()


def request_re_review(db: Session, user_id: UUID) -> None:
    latest = _latest_closed(db, user_id)
    if latest is None or latest["status"] != "upheld":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This account isn't paused"
        )
    if latest["is_appeal"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="The one re-review has been used"
        )
    if _aware(latest["closed_at"]) > _now() - timedelta(days=APPEAL_AFTER_DAYS):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A re-review opens 30 days after the review",
        )
    if _open_review(db, user_id) is not None:
        return
    db.execute(insert(bot_reviews).values(id=uuid4(), target_user_id=user_id, is_appeal=True))
    db.commit()


def review_payload(db: Session, target_id: UUID, viewer_id: UUID | None) -> dict | None:
    """What a profile shows: an open review with its votes, or a paused account's appeal state."""
    standings = compute_standings(db)
    review = _open_review(db, target_id)
    if review is not None and settle(db, review, standings) != "open":
        db.commit()
        standings = compute_standings(db)
        review = None
    if review is not None:
        tally = _tally(db, review, standings)
        viewer_vote = None
        if viewer_id is not None:
            viewer_vote = db.execute(
                select(bot_review_votes.c.vote).where(
                    bot_review_votes.c.review_id == review["id"],
                    bot_review_votes.c.voter_id == viewer_id,
                )
            ).scalar_one_or_none()
        return {
            "status": "open",
            "is_appeal": bool(review["is_appeal"]),
            "bot_votes": tally["bot_votes"],
            "real_votes": tally["real_votes"],
            "votes_required": tally["votes_required"],
            "closes_at": (_aware(review["opened_at"]) + timedelta(days=REVIEW_DAYS)).isoformat(),
            "viewer_vote": viewer_vote,
            "viewer_can_vote": bool(
                viewer_id is not None
                and viewer_id != target_id
                and getattr(standings.get(viewer_id), "level", None) == FULL
            ),
        }
    latest = _latest_closed(db, target_id)
    if latest is not None and latest["status"] == "upheld":
        available = _aware(latest["closed_at"]) + timedelta(days=APPEAL_AFTER_DAYS)
        return {
            "status": "upheld",
            "is_appeal": bool(latest["is_appeal"]),
            "re_review_available_at": None if latest["is_appeal"] else available.isoformat(),
            "can_request_re_review": bool(
                viewer_id == target_id and not latest["is_appeal"] and available <= _now()
            ),
        }
    return None
