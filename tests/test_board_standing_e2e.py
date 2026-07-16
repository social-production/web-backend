from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import board_standing_votes, platform_board_memberships, users


def _request_json(
    url: str,
    method: str = "GET",
    body: dict[str, object] | None = None,
    token: str | None = None,
) -> dict[str, object]:
    payload = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _seed_board_users() -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)

    candidate_id = uuid4()
    voter_id = uuid4()
    member_id = uuid4()

    candidate_name = f"cand-{str(candidate_id)[:8]}"
    voter_name = f"voter-{str(voter_id)[:8]}"
    member_name = f"member-{str(member_id)[:8]}"

    for user_id, username in (
        (candidate_id, candidate_name),
        (voter_id, voter_name),
        (member_id, member_name),
    ):
        db.execute(
            insert(users).values(
                id=user_id,
                username=username,
                email=f"{username}@t.invalid",
                password_hash="x",
                bio="seed",
                created_at=now,
                updated_at=now,
            )
        )

    db.execute(
        insert(platform_board_memberships).values(
            user_id=member_id,
            standing_state="member",
            grace_started_at=None,
            grace_ends_at=None,
            updated_at=now,
        )
    )
    db.execute(
        insert(board_standing_votes).values(
            target_user_id=member_id,
            voter_id=voter_id,
            vote=1,
            updated_at=now,
        )
    )

    db.commit()
    db.close()

    return {
        "candidate_token": create_access_token(str(candidate_id)),
        "candidate_id": str(candidate_id),
        "voter_token": create_access_token(str(voter_id)),
        "voter_id": str(voter_id),
        "member_token": create_access_token(str(member_id)),
        "member_id": str(member_id),
    }


def run() -> None:
    base = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8010")
    seeded = _seed_board_users()

    volunteer = _request_json(
        f"{base}/board/volunteer", method="POST", token=seeded["candidate_token"]
    )
    candidate = volunteer["candidate"]
    assert candidate["membership_state"] == "candidate"

    standing = _request_json(f"{base}/board", token=seeded["voter_token"])
    candidate_ids = {item["user_id"] for item in standing["candidates"]}
    assert seeded["candidate_id"] in candidate_ids

    vote = _request_json(
        f"{base}/board/votes",
        method="POST",
        token=seeded["voter_token"],
        body={"target_user_id": seeded["candidate_id"], "vote": "yes"},
    )
    assert vote["yes_count"] >= 1

    standing_after_vote = _request_json(f"{base}/board", token=seeded["voter_token"])
    member_ids = {item["user_id"] for item in standing_after_vote["members"]}
    assert seeded["candidate_id"] in member_ids
    promoted = next(
        item for item in standing_after_vote["members"] if item["user_id"] == seeded["candidate_id"]
    )
    assert promoted["standing_state"] == "active"
    assert promoted["membership_state"] == "member"

    step_down = _request_json(
        f"{base}/board/volunteer", method="DELETE", token=seeded["candidate_token"]
    )
    assert step_down["removed"] is True

    standing_after_step_down = _request_json(f"{base}/board", token=seeded["voter_token"])
    remaining_member_ids = {item["user_id"] for item in standing_after_step_down["members"]}
    assert seeded["candidate_id"] not in remaining_member_ids

    demotion_vote = _request_json(
        f"{base}/board/votes",
        method="POST",
        token=seeded["candidate_token"],
        body={"target_user_id": seeded["member_id"], "vote": "no"},
    )
    assert demotion_vote["no_count"] >= 1

    standing_after_demotion = _request_json(f"{base}/board", token=seeded["voter_token"])
    remaining_after_demotion = {item["user_id"] for item in standing_after_demotion["members"]}
    assert seeded["member_id"] not in remaining_after_demotion

    print(
        json.dumps(
            {
                "candidate_promoted": seeded["candidate_id"] in member_ids,
                "candidate_stepped_down": seeded["candidate_id"] not in remaining_member_ids,
                "member_demoted": seeded["member_id"] not in remaining_after_demotion,
            }
        )
    )


if __name__ == "__main__":
    run()
