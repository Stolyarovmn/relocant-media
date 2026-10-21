from relocant_media.services.moderation import (
    ModerationAction,
    ModerationContext,
    evaluate_moderation,
    rule_based_moderation,
)


def test_obvious_scam_marker_is_deleted() -> None:
    decision = evaluate_moderation("Доход 100% доход без риска")

    assert decision.action is ModerationAction.DELETE
    assert "percent_income_claim" in decision.reason_codes
    assert decision.audit_reason.startswith("rules-v2:")
    assert rule_based_moderation("Доход 100% доход без риска") is ModerationAction.DELETE


def test_risk_free_income_claim_is_deleted_but_generic_risk_phrase_is_not() -> None:
    assert evaluate_moderation("Инвестиции без риска, гарантированный доход").action is (
        ModerationAction.DELETE
    )
    assert evaluate_moderation("На дороге сегодня без риска не обойтись").action is (
        ModerationAction.KEEP
    )


def test_suspicious_promo_is_reviewed_not_deleted() -> None:
    decision = evaluate_moderation("Есть подработка, пишите мне в лс")

    assert decision.action is ModerationAction.REVIEW
    assert decision.reason_codes == ("suspicious_direct_message_promo",)


def test_targeted_harassment_is_reviewed_not_deleted() -> None:
    decision = evaluate_moderation("Ты идиот, тебе не место в этом городе")

    assert decision.action is ModerationAction.REVIEW
    assert "targeted_harassment" in decision.reason_codes


def test_repeated_same_link_is_deleted_and_many_distinct_links_are_reviewed() -> None:
    repeated = " ".join(["https://example.com/deal"] * 3)
    distinct = " ".join(f"https://example.com/{index}" for index in range(5))

    assert evaluate_moderation(repeated).action is ModerationAction.DELETE
    assert "repeated_same_link" in evaluate_moderation(repeated).reason_codes
    assert evaluate_moderation(distinct).action is ModerationAction.REVIEW
    assert "many_links" in evaluate_moderation(distinct).reason_codes


def test_rate_hints_only_escalate_to_review() -> None:
    decision = evaluate_moderation(
        "Обычное сообщение",
        context=ModerationContext(recent_message_count=9, duplicate_text_count=3),
    )

    assert decision.action is ModerationAction.REVIEW
    assert set(decision.reason_codes) == {
        "duplicate_text_rate_hint",
        "high_message_rate_hint",
    }


def test_criticism_is_not_deleted_by_rule_filter() -> None:
    decision = evaluate_moderation("Ваш пост неточный, проверьте источник")

    assert decision.action is ModerationAction.KEEP
    assert decision.reason_codes == ("no_rule_match",)
