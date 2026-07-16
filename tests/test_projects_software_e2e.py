from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import insert, select

from app.auth.jwt import create_access_token
from app.db import SessionLocal
from app.models import channels, project_memberships, project_plans, projects, users
from app.services.projects_software import sync_merge_capability_for_leading_plan


def _request_json(
    url: str, method: str = "GET", body: dict[str, object] | None = None, token: str | None = None
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


def _request_json_or_closed(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, object] | None = None,
    token: str | None = None,
    expected_closed_detail: str,
) -> dict[str, object] | None:
    try:
        return _request_json(url, method=method, body=body, token=token)
    except urllib.error.HTTPError as exc:
        assert exc.code == 409
        payload = json.loads(exc.read().decode("utf-8"))
        assert payload.get("detail") == expected_closed_detail
        return None


def _request_error_code(
    url: str, method: str = "GET", body: dict[str, object] | None = None, token: str | None = None
) -> int:
    payload = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req):
            raise AssertionError("Expected request to fail")
    except urllib.error.HTTPError as exc:
        return exc.code


def _seed_software_project() -> dict[str, str]:
    db = SessionLocal()
    now = datetime.now(UTC)

    owner_id = uuid4()
    member_id = uuid4()
    channel_id = uuid4()
    project_id = uuid4()

    owner_name = f"softa-{str(owner_id)[:8]}"
    member_name = f"softb-{str(member_id)[:8]}"
    channel_slug = f"softch-{str(channel_id)[:8]}"
    project_slug = f"softproj-{str(project_id)[:8]}"

    db.execute(
        insert(users).values(
            id=owner_id,
            username=owner_name,
            email=f"{owner_name}@t.invalid",
            password_hash="x",
            bio="owner",
            created_at=now,
            updated_at=now,
        )
    )
    db.execute(
        insert(users).values(
            id=member_id,
            username=member_name,
            email=f"{member_name}@t.invalid",
            password_hash="x",
            bio="member",
            created_at=now,
            updated_at=now,
        )
    )

    db.execute(
        insert(channels).values(
            id=channel_id,
            slug=channel_slug,
            name="Software Project Channel",
            description="seed",
            created_by=owner_id,
            created_at=now,
            updated_at=now,
        )
    )

    db.execute(
        insert(projects).values(
            id=project_id,
            slug=project_slug,
            title="Software Project",
            description="Software project description",
            author_id=owner_id,
            project_mode="productive",
            project_subtype="software",
            current_phase_id="phase-3",
            stage_label="distribution-plan",
            location_label="online",
            is_platform_tagged=False,
            is_closed=False,
            signal_count=0,
            vote_count=0,
            comment_count=0,
            member_count=2,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
    )

    db.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=owner_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    db.execute(
        insert(project_memberships).values(
            project_id=project_id,
            user_id=member_id,
            is_manager=False,
            is_manager_candidate=False,
            joined_at=now,
        )
    )
    plan_id = uuid4()
    db.execute(
        insert(project_plans).values(
            id=plan_id,
            project_id=project_id,
            phase_kind="distribution-plan",
            title="Current Plan",
            description="Current plan",
            author_id=owner_id,
            project_subtype="software",
            repository_url="https://github.com/acme/old-repo",
            demand_consideration_note="",
            total_cost_label=None,
            plan_payload={"version": 1},
            is_leading=True,
            status="approved",
            created_at=now,
            updated_at=now,
        )
    )

    sync_merge_capability_for_leading_plan(db, project_id, plan_id)
    db.commit()

    repo_before = db.execute(
        select(project_plans.c.repository_url)
        .where(project_plans.c.project_id == project_id, project_plans.c.is_leading.is_(True))
        .limit(1)
    ).scalar_one()

    db.close()

    return {
        "project_slug": project_slug,
        "owner_token": create_access_token(str(owner_id)),
        "owner_id": str(owner_id),
        "member_token": create_access_token(str(member_id)),
        "member_id": str(member_id),
        "repo_before": repo_before,
    }


def run() -> None:
    base = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8010")
    seeded = _seed_software_project()
    slug = seeded["project_slug"]

    initial_governance = _request_json(
        f"{base}/projects/{slug}/software",
        token=seeded["owner_token"],
    )
    assert any(
        member["id"] == seeded["owner_id"] and member.get("sourceLabel") == "plan-creator"
        for member in initial_governance["mergeCapabilityMembers"]
    ), "Plan creator should receive merge capability automatically"

    pr_submit = _request_json(
        f"{base}/projects/{slug}/software/pull-requests",
        method="POST",
        token=seeded["owner_token"],
        body={
            "title": "PR #1",
            "summary": "Implements feature",
            "pullRequestId": "acme/repo#1",
            "pullRequestUrl": "https://github.com/acme/repo/pull/1",
        },
    )
    pr_request_id = pr_submit["pullRequests"][0]["id"]

    first_pr_vote = _request_json(
        f"{base}/projects/{slug}/software/pull-requests/{pr_request_id}/vote",
        method="POST",
        token=seeded["owner_token"],
        body={"vote": "yes"},
    )
    pr_vote = (
        _request_json_or_closed(
            f"{base}/projects/{slug}/software/pull-requests/{pr_request_id}/vote",
            method="POST",
            token=seeded["member_token"],
            body={"vote": "yes"},
            expected_closed_detail="Pull request is not open for voting",
        )
        or first_pr_vote
    )
    approved_pr = next(item for item in pr_vote["pullRequests"] if item["id"] == pr_request_id)
    assert approved_pr["stage"] == "awaiting-merge"

    merge_forbidden = _request_error_code(
        f"{base}/projects/{slug}/software/pull-requests/{pr_request_id}/merge",
        method="POST",
        token=seeded["member_token"],
        body={"mergeId": "abc123"},
    )
    assert merge_forbidden == 403

    merge_cap_req = _request_json(
        f"{base}/projects/{slug}/software/merge-capability-requests",
        method="POST",
        token=seeded["owner_token"],
        body={
            "targetUserId": seeded["member_id"],
            "action": "grant",
        },
    )
    merge_cap_request_id = merge_cap_req["mergeCapabilityChangeRequests"][0]["id"]

    first_merge_cap_vote = _request_json(
        f"{base}/projects/{slug}/software/merge-capability-requests/{merge_cap_request_id}/vote",
        method="POST",
        token=seeded["owner_token"],
        body={"vote": "yes"},
    )
    merge_cap_vote = (
        _request_json_or_closed(
            f"{base}/projects/{slug}/software/merge-capability-requests/{merge_cap_request_id}/vote",
            method="POST",
            token=seeded["member_token"],
            body={"vote": "yes"},
            expected_closed_detail="Request is already closed",
        )
        or first_merge_cap_vote
    )
    merge_req = next(
        item
        for item in merge_cap_vote["mergeCapabilityChangeRequests"]
        if item["id"] == merge_cap_request_id
    )
    assert merge_req["passesApprovalThreshold"] is True
    assert any(
        member["id"] == seeded["member_id"] for member in merge_cap_vote["mergeCapabilityMembers"]
    )

    merged = _request_json(
        f"{base}/projects/{slug}/software/pull-requests/{pr_request_id}/merge",
        method="POST",
        token=seeded["member_token"],
        body={"mergeId": "abc123"},
    )
    merged_pr = next(item for item in merged["pullRequests"] if item["id"] == pr_request_id)
    assert merged_pr["stage"] == "confirmation"
    assert merged_pr["mergeId"] == "abc123"

    first_confirmation_vote = _request_json(
        f"{base}/projects/{slug}/software/pull-requests/{pr_request_id}/vote",
        method="POST",
        token=seeded["owner_token"],
        body={"vote": "yes"},
    )
    confirmation_vote = (
        _request_json_or_closed(
            f"{base}/projects/{slug}/software/pull-requests/{pr_request_id}/vote",
            method="POST",
            token=seeded["member_token"],
            body={"vote": "yes"},
            expected_closed_detail="Pull request is not open for voting",
        )
        or first_confirmation_vote
    )
    merged_pr = next(
        item for item in confirmation_vote["pullRequests"] if item["id"] == pr_request_id
    )
    assert merged_pr["stage"] == "confirmed"

    repo_req = _request_json(
        f"{base}/projects/{slug}/software/repository-replacement-requests",
        method="POST",
        token=seeded["owner_token"],
        body={
            "repositoryUrl": "https://github.com/acme/new-repo",
            "reason": "Repository migration",
            "relatedPullRequestId": pr_request_id,
        },
    )
    repo_request_id = repo_req["repositoryReplacementRequests"][0]["id"]

    first_repo_vote = _request_json(
        f"{base}/projects/{slug}/software/repository-replacement-requests/{repo_request_id}/vote",
        method="POST",
        token=seeded["owner_token"],
        body={"vote": "yes"},
    )
    repo_vote = (
        _request_json_or_closed(
            f"{base}/projects/{slug}/software/repository-replacement-requests/{repo_request_id}/vote",
            method="POST",
            token=seeded["member_token"],
            body={"vote": "yes"},
            expected_closed_detail="Request is already closed",
        )
        or first_repo_vote
    )
    repo_decision = next(
        item for item in repo_vote["repositoryReplacementRequests"] if item["id"] == repo_request_id
    )
    assert repo_decision["passesApprovalThreshold"] is True

    db = SessionLocal()
    repo_after = db.execute(
        select(project_plans.c.repository_url)
        .join(projects, projects.c.id == project_plans.c.project_id)
        .where(projects.c.slug == slug, project_plans.c.is_leading.is_(True))
        .limit(1)
    ).scalar_one()
    db.close()

    assert repo_after == "https://github.com/acme/new-repo"

    print(
        json.dumps(
            {
                "project_slug": slug,
                "pull_request_status": merged_pr["stage"],
                "merge_recorded": merged_pr["mergeId"] is not None,
                "merge_capability_granted": any(
                    member["id"] == seeded["member_id"]
                    for member in merge_cap_vote["mergeCapabilityMembers"]
                ),
                "repository_replaced": repo_after != seeded["repo_before"],
                "repository_before": seeded["repo_before"],
                "repository_after": repo_after,
            }
        )
    )


if __name__ == "__main__":
    run()
