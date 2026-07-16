from app.services.governance_votes import APPROVAL_THRESHOLD, compute_software_vote_summary


def test_compute_software_vote_summary_active_vote():
    rows = [{"vote": "yes", "voter_id": "user-a"}]
    summary, passes, can_still_pass = compute_software_vote_summary(
        rows, member_count=5, current_user_id="user-a"
    )
    assert summary["activeVote"] == "yes"
    assert summary["yesCount"] == 1
    assert isinstance(passes, bool)
    assert isinstance(can_still_pass, bool)


def test_approval_threshold_default():
    assert APPROVAL_THRESHOLD == 0.66
