from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services import standing as standing_module
from app.utils import votes


def test_full_member_votes_even_before_the_new_account_warm_up(monkeypatch):
    # The platform's first accounts start full before they'd pass the warm-up rule.
    monkeypatch.setattr(
        standing_module, "standing_for", lambda db, user_id: standing_module.Standing("full", 1.0)
    )
    monkeypatch.setattr(votes, "_layer2_allows", lambda db, user_id: False)
    votes.ensure_established_voter(None, uuid4())
    assert votes.is_established_voter(None, uuid4())


def test_new_account_is_refused_with_the_reason(monkeypatch):
    monkeypatch.setattr(
        standing_module, "standing_for", lambda db, user_id: standing_module.Standing("new", 0.0)
    )
    monkeypatch.setattr(
        votes, "warmup_gap", lambda db, user_id, action="voting": f"Warm up before {action}."
    )
    with pytest.raises(HTTPException) as refused:
        votes.ensure_established_voter(None, uuid4())
    assert refused.value.detail == "Warm up before voting."
