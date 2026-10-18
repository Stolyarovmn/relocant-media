from relocant_media.services.moderation import ModerationAction, rule_based_moderation


def test_obvious_scam_marker_is_deleted() -> None:
    assert rule_based_moderation("Доход 100% доход без риска") is ModerationAction.DELETE


def test_criticism_is_not_deleted_by_rule_filter() -> None:
    assert rule_based_moderation("Ваш пост неточный, проверьте источник") is ModerationAction.KEEP
