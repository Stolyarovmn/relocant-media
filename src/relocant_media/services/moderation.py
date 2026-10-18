from enum import StrEnum


class ModerationAction(StrEnum):
    KEEP = "keep"
    DELETE = "delete"
    REVIEW = "review"


OBVIOUS_SPAM_MARKERS = (
    "guaranteed profit",
    "100% доход",
    "без риска",
    "пишите в лс заработок",
)


def rule_based_moderation(text: str) -> ModerationAction:
    normalized = " ".join(text.lower().split())
    if any(marker in normalized for marker in OBVIOUS_SPAM_MARKERS):
        return ModerationAction.DELETE
    if len(normalized) > 4000:
        return ModerationAction.REVIEW
    return ModerationAction.KEEP
