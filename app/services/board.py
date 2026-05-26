from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID

from fastapi import HTTPException, status
from redis import Redis as SyncRedis
from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import board_standing_votes, meaningful_actions, platform_board_memberships, users
from app.services.meaningful_actions import record_meaningful_action
from app.utils.votes import required_votes

BOARD_STATE_MEMBER = "member"
BOARD_STATE_CANDIDATE = "candidate"
VALID_BOARD_STATES = frozenset({BOARD_STATE_MEMBER, BOARD_STATE_CANDIDATE})
MIN_APPROVAL_RATIO = 0.66
VOTE_VALUE_MAP = {"yes": 1, "no": -1}
WEEKLY_ACTIVE_CACHE_KEY = "governance:weekly_active"
WEEKLY_ACTIVE_CACHE_TTL_SECONDS = 3600


@lru_cache(maxsize=1)
def _redis_client() -> SyncRedis:
    settings = get_settings()
    return SyncRedis.from_url(settings.redis_url, decode_responses=True)


def _weekly_active_users(db: Session) -> int:
    try:
        cached = _redis_client().get(WEEKLY_ACTIVE_CACHE_KEY)
        if cached is not None:
            return max(0, int(cached))
    except Exception:
        # Cache errors should not block governance calculations.
        pass

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    total = db.execute(
        select(func.count(func.distinct(meaningful_actions.c.user_id))).where(
            meaningful_actions.c.occurred_at >= week_ago
        )
    ).scalar_one()
    computed = int(total or 0)

    try:
        _redis_client().setex(WEEKLY_ACTIVE_CACHE_KEY, WEEKLY_ACTIVE_CACHE_TTL_SECONDS, computed)
    except Exception:
        # Cache write failures are non-fatal.
        pass

    return computed


def _vote_stats_map(db: Session, target_user_ids: list[UUID]) -> dict[UUID, dict[str, object]]:
    if not target_user_ids:
        return {}

    yes_count_expr = func.coalesce(func.sum(case((board_standing_votes.c.vote == 1, 1), else_=0)), 0)
    no_count_expr = func.coalesce(func.sum(case((board_standing_votes.c.vote == -1, 1), else_=0)), 0)

    rows = db.execute(
        select(
            board_standing_votes.c.target_user_id,
            yes_count_expr.label("yes_count"),
            no_count_expr.label("no_count"),
        )
        .where(board_standing_votes.c.target_user_id.in_(target_user_ids))
        .group_by(board_standing_votes.c.target_user_id)
    ).mappings().all()

    stats: dict[UUID, dict[str, object]] = {}
    for row in rows:
        yes_count = int(row["yes_count"] or 0)
        no_count = int(row["no_count"] or 0)
        vote_count = yes_count + no_count
        approval_ratio = (yes_count / vote_count) if vote_count > 0 else 1.0
        stats[row["target_user_id"]] = {
            "yes_count": yes_count,
            "no_count": no_count,
            "vote_count": vote_count,
            "approval_ratio": round(approval_ratio, 4),
        }
    return stats


def _remove_unqualified_members(db: Session) -> tuple[list[UUID], int, int]:
    weekly_active_users = _weekly_active_users(db)
    required_quorum = required_votes(weekly_active_users)

    member_ids = [
        row["user_id"]
        for row in db.execute(
            select(platform_board_memberships.c.user_id).where(
                platform_board_memberships.c.standing_state == BOARD_STATE_MEMBER
            )
        ).mappings().all()
    ]

    if not member_ids:
        return [], weekly_active_users, required_quorum

    stats = _vote_stats_map(db, member_ids)
    remove_ids: list[UUID] = []

    for member_id in member_ids:
        member_stats = stats.get(
            member_id,
            {"yes_count": 0, "no_count": 0, "vote_count": 0, "approval_ratio": 1.0},
        )
        vote_count = int(member_stats["vote_count"])
        approval_ratio = float(member_stats["approval_ratio"])

        if vote_count < required_quorum or (vote_count > 0 and approval_ratio < MIN_APPROVAL_RATIO):
            remove_ids.append(member_id)

    if remove_ids:
        try:
            db.execute(
                delete(platform_board_memberships).where(
                    platform_board_memberships.c.user_id.in_(remove_ids),
                    platform_board_memberships.c.standing_state == BOARD_STATE_MEMBER,
                )
            )
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not prune board members") from exc

    return remove_ids, weekly_active_users, required_quorum


def _serialize_board_profile(
    row: Mapping[str, object],
    stats_map: dict[UUID, dict[str, object]],
) -> dict[str, object]:
    user_id = row["user_id"]
    stats = stats_map.get(
        user_id,
        {"yes_count": 0, "no_count": 0, "vote_count": 0, "approval_ratio": 1.0},
    )

    return {
        "user_id": user_id,
        "username": row["username"],
        "standing_state": row["standing_state"],
        "updated_at": row["updated_at"],
        "yes_count": int(stats["yes_count"]),
        "no_count": int(stats["no_count"]),
        "vote_count": int(stats["vote_count"]),
        "approval_ratio": float(stats["approval_ratio"]),
    }


def _board_rows_with_users(db: Session) -> list[Mapping[str, object]]:
    return db.execute(
        select(
            platform_board_memberships.c.user_id,
            platform_board_memberships.c.standing_state,
            platform_board_memberships.c.updated_at,
            users.c.username,
        )
        .join(users, users.c.id == platform_board_memberships.c.user_id)
        .where(platform_board_memberships.c.standing_state.in_(VALID_BOARD_STATES))
        .order_by(platform_board_memberships.c.updated_at.desc())
    ).mappings().all()


def volunteer_as_candidate(db: Session, current_user_id: UUID) -> dict[str, object]:
    existing = db.execute(
        select(platform_board_memberships.c.user_id, platform_board_memberships.c.standing_state)
        .where(platform_board_memberships.c.user_id == current_user_id)
    ).mappings().first()

    try:
        if existing is None:
            db.execute(
                insert(platform_board_memberships).values(
                    user_id=current_user_id,
                    standing_state=BOARD_STATE_CANDIDATE,
                    grace_started_at=None,
                    grace_ends_at=None,
                )
            )
        elif existing["standing_state"] != BOARD_STATE_MEMBER:
            db.execute(
                update(platform_board_memberships)
                .where(platform_board_memberships.c.user_id == current_user_id)
                .values(standing_state=BOARD_STATE_CANDIDATE)
            )

        removed_member_ids, weekly_active_users, required_quorum = _remove_unqualified_members(db)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not volunteer") from exc

    rows = _board_rows_with_users(db)
    stats_map = _vote_stats_map(db, [row["user_id"] for row in rows])
    profile_row = next((row for row in rows if row["user_id"] == current_user_id), None)
    if profile_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    return {
        "candidate": _serialize_board_profile(profile_row, stats_map),
        "weekly_active_users": weekly_active_users,
        "required_quorum": required_quorum,
        "removed_member_ids": removed_member_ids,
    }


def cast_standing_vote(
    db: Session,
    current_user_id: UUID,
    target_user_id: UUID,
    vote: str,
) -> dict[str, object]:
    normalized_vote = vote.strip().lower()
    if normalized_vote not in VOTE_VALUE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="vote must be one of: ['no', 'yes']",
        )

    target = db.execute(
        select(
            platform_board_memberships.c.user_id,
            platform_board_memberships.c.standing_state,
        ).where(platform_board_memberships.c.user_id == target_user_id)
    ).mappings().first()
    if target is None or target["standing_state"] not in VALID_BOARD_STATES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board candidate/member not found")

    value = VOTE_VALUE_MAP[normalized_vote]

    existing_vote = db.execute(
        select(board_standing_votes.c.target_user_id, board_standing_votes.c.voter_id)
        .where(
            board_standing_votes.c.target_user_id == target_user_id,
            board_standing_votes.c.voter_id == current_user_id,
        )
    ).mappings().first()

    try:
        if existing_vote is None:
            db.execute(
                insert(board_standing_votes).values(
                    target_user_id=target_user_id,
                    voter_id=current_user_id,
                    vote=value,
                )
            )
        else:
            db.execute(
                update(board_standing_votes)
                .where(
                    board_standing_votes.c.target_user_id == target_user_id,
                    board_standing_votes.c.voter_id == current_user_id,
                )
                .values(vote=value)
            )

        removed_member_ids, weekly_active_users, required_quorum = _remove_unqualified_members(db)
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={"target_type": "board-standing", "target_id": str(target_user_id), "vote": normalized_vote},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not cast vote") from exc

    stats_map = _vote_stats_map(db, [target_user_id])
    target_stats = stats_map.get(
        target_user_id,
        {"yes_count": 0, "no_count": 0, "vote_count": 0, "approval_ratio": 1.0},
    )

    return {
        "target_user_id": target_user_id,
        "vote": normalized_vote,
        "yes_count": int(target_stats["yes_count"]),
        "no_count": int(target_stats["no_count"]),
        "vote_count": int(target_stats["vote_count"]),
        "approval_ratio": float(target_stats["approval_ratio"]),
        "weekly_active_users": weekly_active_users,
        "required_quorum": required_quorum,
        "removed_member_ids": removed_member_ids,
    }


def list_board_standing(db: Session) -> dict[str, object]:
    removed_member_ids, weekly_active_users, required_quorum = _remove_unqualified_members(db)
    if removed_member_ids:
        db.commit()

    rows = _board_rows_with_users(db)
    stats_map = _vote_stats_map(db, [row["user_id"] for row in rows])

    members: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []

    for row in rows:
        serialized = _serialize_board_profile(row, stats_map)
        if row["standing_state"] == BOARD_STATE_MEMBER:
            members.append(serialized)
        elif row["standing_state"] == BOARD_STATE_CANDIDATE:
            candidates.append(serialized)

    return {
        "weekly_active_users": weekly_active_users,
        "required_quorum": required_quorum,
        "members": members,
        "candidates": candidates,
        "total_members": len(members),
        "total_candidates": len(candidates),
        "removed_member_ids": removed_member_ids,
    }
