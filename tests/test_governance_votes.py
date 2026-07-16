from app.services.governance_votes import APPROVAL_THRESHOLD
from app.utils.votes import required_votes


def test_governance_vote_threshold():
    assert APPROVAL_THRESHOLD == 0.66


def test_required_votes_for_small_population():
    assert required_votes(2) == 2
