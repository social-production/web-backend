from app.services.notification_preferences import (
    DEFAULT_NOTIFICATION_CATEGORIES,
    allowed_notification_kinds,
    category_for_kind,
    normalize_notification_categories,
)


def test_defaults_omit_plan_leading() -> None:
    assert "plan_leading" not in DEFAULT_NOTIFICATION_CATEGORIES
    assert normalize_notification_categories(None) == list(DEFAULT_NOTIFICATION_CATEGORIES)


def test_plan_lead_kinds_map_to_plan_leading() -> None:
    assert category_for_kind("evt-plan-lead") == "plan_leading"
    assert category_for_kind("prj-plan-lead") == "plan_leading"
    assert "evt-plan-lead" not in allowed_notification_kinds(None)
    assert "prj-plan-lead" not in allowed_notification_kinds(None)


def test_normalize_keeps_order_and_drops_unknown() -> None:
    assert normalize_notification_categories(["comments", "bogus", "follows", "comments"]) == [
        "comments",
        "follows",
    ]
    assert normalize_notification_categories([]) == []
