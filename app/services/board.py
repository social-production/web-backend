from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from redis import Redis as SyncRedis
from sqlalchemy import case, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cache import get_sync_redis_client
from app.models import board_standing_votes, meaningful_actions, platform_board_memberships, users
from app.services.meaningful_actions import record_meaningful_action
from app.utils.votes import required_votes

BOARD_STATE_MEMBER = "member"
BOARD_STATE_CANDIDATE = "candidate"
VALID_BOARD_STATES = frozenset({BOARD_STATE_MEMBER, BOARD_STATE_CANDIDATE})
MIN_APPROVAL_RATIO = 0.66
VOTE_VALUE_MAP = {"yes": 1, "no": -1}
WEEKLY_ACTIVE_CACHE_KEY = "board:weekly_active"
WEEKLY_ACTIVE_CACHE_TTL_SECONDS = 3600
GRACE_PERIOD_DAYS = 7

STANDING_ACTIVE = "active"
STANDING_GRACE = "grace"
STANDING_BELOW_THRESHOLD = "below-threshold"
STANDING_QUALIFYING = "qualifying"


def _redis_client() -> SyncRedis:
    return get_sync_redis_client()


def _weekly_active_users(db: Session) -> int:
    try:
        cached = _redis_client().get(WEEKLY_ACTIVE_CACHE_KEY)
        if cached is not None:
            return max(0, int(cached))
    except Exception:
        pass

    week_ago = datetime.now(UTC) - timedelta(days=7)
    try:
        total = db.execute(
            select(func.count(meaningful_actions.c.user_id.distinct())).where(
                meaningful_actions.c.occurred_at >= week_ago
            )
        ).scalar_one()
        computed = int(total or 0)
    except Exception:
        computed = 0

    try:
        _redis_client().setex(WEEKLY_ACTIVE_CACHE_KEY, WEEKLY_ACTIVE_CACHE_TTL_SECONDS, computed)
    except Exception:
        pass

    return computed


def _vote_stats_map(db: Session, target_user_ids: list[UUID]) -> dict[UUID, dict[str, object]]:
    if not target_user_ids:
        return {}

    yes_count_expr = func.coalesce(
        func.sum(case((board_standing_votes.c.vote == 1, 1), else_=0)), 0
    )
    no_count_expr = func.coalesce(
        func.sum(case((board_standing_votes.c.vote == -1, 1), else_=0)), 0
    )

    rows = (
        db.execute(
            select(
                board_standing_votes.c.target_user_id,
                yes_count_expr.label("yes_count"),
                no_count_expr.label("no_count"),
            )
            .where(board_standing_votes.c.target_user_id.in_(target_user_ids))
            .group_by(board_standing_votes.c.target_user_id)
        )
        .mappings()
        .all()
    )

    stats: dict[UUID, dict[str, object]] = {}
    for row in rows:
        yes_count = int(row["yes_count"] or 0)
        no_count = int(row["no_count"] or 0)
        vote_count = yes_count + no_count
        approval_ratio = (yes_count / vote_count) if vote_count > 0 else 0.0
        stats[row["target_user_id"]] = {
            "yes_count": yes_count,
            "no_count": no_count,
            "vote_count": vote_count,
            "approval_ratio": round(approval_ratio, 4),
        }
    return stats


def _member_stats(
    stats_map: dict[UUID, dict[str, object]],
    user_id: UUID,
) -> dict[str, object]:
    return stats_map.get(
        user_id,
        {"yes_count": 0, "no_count": 0, "vote_count": 0, "approval_ratio": 0.0},
    )


def _meets_standing_threshold(
    stats: dict[str, object],
    required_quorum: int,
) -> bool:
    vote_count = int(stats["vote_count"])
    approval_ratio = float(stats["approval_ratio"])
    return vote_count >= required_quorum and (
        vote_count == 0 or approval_ratio >= MIN_APPROVAL_RATIO
    )


def _compute_standing_state(
    *,
    db_state: str,
    stats: dict[str, object],
    required_quorum: int,
    grace_started_at: datetime | None,
    grace_ends_at: datetime | None,
) -> str:
    vote_count = int(stats["vote_count"])
    approval_ratio = float(stats["approval_ratio"])
    now = datetime.now(UTC)

    if db_state == BOARD_STATE_CANDIDATE:
        if _meets_standing_threshold(stats, required_quorum):
            return STANDING_QUALIFYING
        return STANDING_BELOW_THRESHOLD

    if vote_count > 0 and approval_ratio < MIN_APPROVAL_RATIO:
        return STANDING_BELOW_THRESHOLD

    if vote_count >= required_quorum:
        return STANDING_ACTIVE

    if grace_ends_at is not None and grace_ends_at >= now:
        return STANDING_GRACE

    return STANDING_BELOW_THRESHOLD


def _promote_qualified_candidates(
    db: Session,
    stats_map: dict[UUID, dict[str, object]],
    required_quorum: int,
) -> list[UUID]:
    candidate_ids = [
        row["user_id"]
        for row in db.execute(
            select(platform_board_memberships.c.user_id).where(
                platform_board_memberships.c.standing_state == BOARD_STATE_CANDIDATE
            )
        )
        .mappings()
        .all()
    ]

    promoted: list[UUID] = []
    for candidate_id in candidate_ids:
        stats = _member_stats(stats_map, candidate_id)
        if _meets_standing_threshold(stats, required_quorum):
            db.execute(
                update(platform_board_memberships)
                .where(platform_board_memberships.c.user_id == candidate_id)
                .values(
                    standing_state=BOARD_STATE_MEMBER,
                    grace_started_at=None,
                    grace_ends_at=None,
                )
            )
            promoted.append(candidate_id)

    return promoted


def _remove_unqualified_members(
    db: Session,
    stats_map: dict[UUID, dict[str, object]],
    required_quorum: int,
) -> list[UUID]:
    now = datetime.now(UTC)
    member_rows = (
        db.execute(
            select(
                platform_board_memberships.c.user_id,
                platform_board_memberships.c.grace_started_at,
                platform_board_memberships.c.grace_ends_at,
            ).where(platform_board_memberships.c.standing_state == BOARD_STATE_MEMBER)
        )
        .mappings()
        .all()
    )

    remove_ids: list[UUID] = []

    for row in member_rows:
        member_id = row["user_id"]
        stats = _member_stats(stats_map, member_id)
        vote_count = int(stats["vote_count"])
        approval_ratio = float(stats["approval_ratio"])

        if vote_count > 0 and approval_ratio < MIN_APPROVAL_RATIO:
            remove_ids.append(member_id)
            continue

        if vote_count >= required_quorum:
            if row["grace_started_at"] is not None or row["grace_ends_at"] is not None:
                db.execute(
                    update(platform_board_memberships)
                    .where(platform_board_memberships.c.user_id == member_id)
                    .values(grace_started_at=None, grace_ends_at=None)
                )
            continue

        grace_ends_at = row["grace_ends_at"]
        if grace_ends_at is not None and grace_ends_at >= now:
            continue

        if grace_ends_at is None:
            grace_end = now + timedelta(days=GRACE_PERIOD_DAYS)
            db.execute(
                update(platform_board_memberships)
                .where(platform_board_memberships.c.user_id == member_id)
                .values(grace_started_at=now, grace_ends_at=grace_end)
            )
            continue

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
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not prune board members",
            ) from exc

    return remove_ids


def _reconcile_board_standing(db: Session) -> tuple[list[UUID], list[UUID], int, int]:
    weekly_active_users = _weekly_active_users(db)
    required_quorum = required_votes(weekly_active_users)

    all_ids = [
        row["user_id"]
        for row in db.execute(select(platform_board_memberships.c.user_id)).mappings().all()
    ]
    stats_map = _vote_stats_map(db, all_ids)

    promoted_ids = _promote_qualified_candidates(db, stats_map, required_quorum)
    if promoted_ids:
        stats_map = _vote_stats_map(db, all_ids)

    removed_member_ids = _remove_unqualified_members(db, stats_map, required_quorum)
    return promoted_ids, removed_member_ids, weekly_active_users, required_quorum


def _sync_platform_merge_capability_after_board_change(db: Session) -> None:
    try:
        from app.services.projects_software import sync_platform_software_merge_capability

        sync_platform_software_merge_capability(db)
    except Exception:
        pass


def _serialize_board_profile(
    row: Mapping[str, object],
    stats_map: dict[UUID, dict[str, object]],
    required_quorum: int = 0,
    weekly_active_users: int = 0,
    active_vote: str | None = None,
) -> dict[str, object]:
    user_id = row["user_id"]
    stats = _member_stats(stats_map, user_id)
    grace_started_at = row.get("grace_started_at")
    grace_ends_at = row.get("grace_ends_at")
    db_state = str(row["standing_state"])

    standing_state = _compute_standing_state(
        db_state=db_state,
        stats=stats,
        required_quorum=required_quorum,
        grace_started_at=grace_started_at if isinstance(grace_started_at, datetime) else None,
        grace_ends_at=grace_ends_at if isinstance(grace_ends_at, datetime) else None,
    )

    return {
        "user_id": user_id,
        "username": row["username"],
        "standing_state": standing_state,
        "membership_state": db_state,
        "updated_at": row["updated_at"],
        "yes_count": int(stats["yes_count"]),
        "no_count": int(stats["no_count"]),
        "vote_count": int(stats["vote_count"]),
        "approval_ratio": float(stats["approval_ratio"]),
        "required_quorum": required_quorum,
        "weekly_active_users": weekly_active_users,
        "grace_ends_at": grace_ends_at,
        "active_vote": {"1": "yes", "-1": "no", "yes": "yes", "no": "no"}.get(str(active_vote))
        if active_vote
        else None,
    }


def _board_rows_with_users(db: Session) -> list[Mapping[str, object]]:
    return (
        db.execute(
            select(
                platform_board_memberships.c.user_id,
                platform_board_memberships.c.standing_state,
                platform_board_memberships.c.grace_started_at,
                platform_board_memberships.c.grace_ends_at,
                platform_board_memberships.c.updated_at,
                users.c.username,
            )
            .join(users, users.c.id == platform_board_memberships.c.user_id)
            .where(platform_board_memberships.c.standing_state.in_(VALID_BOARD_STATES))
            .order_by(platform_board_memberships.c.updated_at.desc())
        )
        .mappings()
        .all()
    )


def get_active_board_member_ids(db: Session) -> list[UUID]:
    return [
        row["user_id"]
        for row in db.execute(
            select(platform_board_memberships.c.user_id).where(
                platform_board_memberships.c.standing_state == BOARD_STATE_MEMBER
            )
        )
        .mappings()
        .all()
    ]


def list_active_board_member_ids(db: Session) -> list[UUID]:
    _reconcile_board_standing(db)
    db.commit()
    return get_active_board_member_ids(db)


def volunteer_as_candidate(db: Session, current_user_id: UUID) -> dict[str, object]:
    existing = (
        db.execute(
            select(
                platform_board_memberships.c.user_id, platform_board_memberships.c.standing_state
            ).where(platform_board_memberships.c.user_id == current_user_id)
        )
        .mappings()
        .first()
    )

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
                .values(
                    standing_state=BOARD_STATE_CANDIDATE,
                    grace_started_at=None,
                    grace_ends_at=None,
                )
            )

        promoted_ids, removed_member_ids, weekly_active_users, required_quorum = (
            _reconcile_board_standing(db)
        )
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="volunteer-board",
            metadata={"standing_state": BOARD_STATE_CANDIDATE},
        )
        db.commit()
        if promoted_ids or removed_member_ids:
            _sync_platform_merge_capability_after_board_change(db)
            db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not volunteer"
        ) from exc

    rows = _board_rows_with_users(db)
    stats_map = _vote_stats_map(db, [row["user_id"] for row in rows])
    profile_row = next((row for row in rows if row["user_id"] == current_user_id), None)
    if profile_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    return {
        "candidate": _serialize_board_profile(
            profile_row,
            stats_map,
            required_quorum=required_quorum,
            weekly_active_users=weekly_active_users,
        ),
        "weekly_active_users": weekly_active_users,
        "required_quorum": required_quorum,
        "removed_member_ids": removed_member_ids,
        "promoted_member_ids": promoted_ids,
    }


def remove_volunteer(db: Session, current_user_id: UUID) -> dict[str, object]:
    row = (
        db.execute(
            select(
                platform_board_memberships.c.user_id, platform_board_memberships.c.standing_state
            ).where(platform_board_memberships.c.user_id == current_user_id)
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"removed": False, "detail": "Not a board volunteer or member"}

    was_member = row["standing_state"] == BOARD_STATE_MEMBER

    db.execute(
        delete(platform_board_memberships).where(
            platform_board_memberships.c.user_id == current_user_id
        )
    )
    db.commit()

    if was_member:
        _sync_platform_merge_capability_after_board_change(db)
        db.commit()

    return {"removed": True, "was_member": was_member}


def cast_standing_vote(
    db: Session,
    current_user_id: UUID,
    target_user_id: UUID,
    vote: str,
) -> dict[str, object]:
    normalized_vote = vote.strip().lower()
    if normalized_vote == "neutral":
        try:
            db.execute(
                delete(board_standing_votes).where(
                    board_standing_votes.c.target_user_id == target_user_id,
                    board_standing_votes.c.voter_id == current_user_id,
                )
            )
            promoted_ids, removed_member_ids, weekly_active_users, required_quorum = (
                _reconcile_board_standing(db)
            )
            db.commit()
            if promoted_ids or removed_member_ids:
                _sync_platform_merge_capability_after_board_change(db)
                db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not remove vote"
            ) from exc

        stats_map = _vote_stats_map(db, [target_user_id])
        target_stats = _member_stats(stats_map, target_user_id)

        return {
            "target_user_id": target_user_id,
            "vote": "neutral",
            "yes_count": int(target_stats["yes_count"]),
            "no_count": int(target_stats["no_count"]),
            "vote_count": int(target_stats["vote_count"]),
            "approval_ratio": float(target_stats["approval_ratio"]),
            "weekly_active_users": weekly_active_users,
            "required_quorum": required_quorum,
            "removed_member_ids": removed_member_ids,
            "promoted_member_ids": promoted_ids,
        }

    if normalized_vote not in VOTE_VALUE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="vote must be one of: ['no', 'yes']",
        )

    target = (
        db.execute(
            select(
                platform_board_memberships.c.user_id,
                platform_board_memberships.c.standing_state,
            ).where(platform_board_memberships.c.user_id == target_user_id)
        )
        .mappings()
        .first()
    )
    if target is None or target["standing_state"] not in VALID_BOARD_STATES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Board candidate/member not found"
        )

    value = VOTE_VALUE_MAP[normalized_vote]

    existing_vote = (
        db.execute(
            select(board_standing_votes.c.target_user_id, board_standing_votes.c.voter_id).where(
                board_standing_votes.c.target_user_id == target_user_id,
                board_standing_votes.c.voter_id == current_user_id,
            )
        )
        .mappings()
        .first()
    )

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

        promoted_ids, removed_member_ids, weekly_active_users, required_quorum = (
            _reconcile_board_standing(db)
        )
        record_meaningful_action(
            db=db,
            user_id=current_user_id,
            action_type="cast-vote",
            metadata={
                "target_type": "board-standing",
                "target_id": str(target_user_id),
                "vote": normalized_vote,
            },
        )
        db.commit()
        if promoted_ids or removed_member_ids:
            _sync_platform_merge_capability_after_board_change(db)
            db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not cast vote"
        ) from exc

    stats_map = _vote_stats_map(db, [target_user_id])
    target_stats = _member_stats(stats_map, target_user_id)

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
        "promoted_member_ids": promoted_ids,
    }


def list_board_standing(
    db: Session,
    viewer_user_id: UUID | None = None,
) -> dict[str, object]:
    promoted_ids, removed_member_ids, weekly_active_users, required_quorum = (
        _reconcile_board_standing(db)
    )
    if promoted_ids or removed_member_ids:
        db.commit()
        if promoted_ids or removed_member_ids:
            _sync_platform_merge_capability_after_board_change(db)
            db.commit()

    rows = _board_rows_with_users(db)
    stats_map = _vote_stats_map(db, [row["user_id"] for row in rows])

    active_votes: dict[UUID, str] = {}
    if viewer_user_id is not None:
        vote_rows = db.execute(
            select(board_standing_votes.c.target_user_id, board_standing_votes.c.vote).where(
                board_standing_votes.c.voter_id == viewer_user_id
            )
        ).all()
        for target_user_id, vote_value in vote_rows:
            active_votes[target_user_id] = vote_value

    members: list[dict[str, object]] = []
    candidates: list[dict[str, object]] = []

    for row in rows:
        serialized = _serialize_board_profile(
            row,
            stats_map,
            required_quorum=required_quorum,
            weekly_active_users=weekly_active_users,
            active_vote=active_votes.get(row["user_id"]),
        )
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
        "promoted_member_ids": promoted_ids,
    }
