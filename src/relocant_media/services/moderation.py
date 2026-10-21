from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum


class ModerationAction(StrEnum):
    KEEP = "keep"
    DELETE = "delete"
    REVIEW = "review"


@dataclass(frozen=True, slots=True)
class ModerationContext:
    """Optional deterministic rate hints supplied by a trusted runtime."""

    recent_message_count: int | None = None
    duplicate_text_count: int | None = None


@dataclass(frozen=True, slots=True)
class ModerationDecision:
    action: ModerationAction
    reason_codes: tuple[str, ...]

    @property
    def audit_reason(self) -> str:
        codes = self.reason_codes or ("no_rule_match",)
        return f"rules-v2:{','.join(codes)}"


_URL_RE = re.compile(
    r"(?i)\b(?:https?://|www\.|t\.me/)[^\s<>]+",
)

# Only high-confidence scam/spam patterns are auto-deleted. Ambiguous commercial,
# harassment and rate signals intentionally fall through to REVIEW.
_DELETE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "guaranteed_profit",
        re.compile(r"(?i)\b(?:guaranteed\s+profit|гарантированн\w*\s+(?:доход|прибыл\w*))\b"),
    ),
    (
        "percent_income_claim",
        re.compile(r"(?i)\b(?:100\s*%|100процентов)\s+(?:доход|прибыл\w*|заработ\w*)\b"),
    ),
    (
        "risk_free_income_claim",
        re.compile(
            r"(?i)(?:\b(?:доход|прибыл\w*|заработ\w*|инвест\w*)\b.{0,45}\bбез\s+риска\b|"
            r"\bбез\s+риска\b.{0,45}\b(?:доход|прибыл\w*|заработ\w*|инвест\w*)\b)"
        ),
    ),
)

_REVIEW_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "suspicious_direct_message_promo",
        re.compile(
            r"(?i)(?:пиш(?:и|ите)\s+(?:мне\s+)?(?:в\s+)?(?:лс|личк\w*)|"
            r"write\s+(?:me\s+)?(?:in\s+)?dm)"
        ),
    ),
    (
        "suspicious_fast_money",
        re.compile(r"(?i)\b(?:быстр\w*\s+заработ\w*|л[её]гк\w*\s+деньг\w*|easy\s+money)\b"),
    ),
    (
        "targeted_harassment",
        re.compile(
            r"(?i)\b(?:убью\s+тебя|тебе\s+не\s+место|сдохни|идиот\w*|"
            r"i\s+will\s+kill\s+you|you\s+don'?t\s+belong)\b"
        ),
    ),
)


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _normalized_urls(text: str) -> list[str]:
    urls: list[str] = []
    for match in _URL_RE.findall(text):
        urls.append(match.rstrip(".,;:!?)]}\"'").casefold())
    return urls


def evaluate_moderation(
    text: str,
    *,
    context: ModerationContext | None = None,
) -> ModerationDecision:
    """Return a deterministic moderation decision with stable reason codes.

    DELETE is reserved for high-confidence scam/spam signals. Signals that depend on
    tone, promotion intent or rate thresholds are REVIEW-only to avoid deleting
    legitimate criticism, recommendations or heated but non-spam discussion.
    """
    normalized = _normalize(text)
    delete_reasons: list[str] = []
    review_reasons: list[str] = []

    for reason, pattern in _DELETE_PATTERNS:
        if pattern.search(normalized):
            delete_reasons.append(reason)

    urls = _normalized_urls(text)
    if urls:
        counts = Counter(urls)
        if max(counts.values()) >= 3:
            delete_reasons.append("repeated_same_link")
        elif len(urls) >= 5:
            review_reasons.append("many_links")

    for reason, pattern in _REVIEW_PATTERNS:
        if pattern.search(normalized):
            review_reasons.append(reason)

    if len(normalized) > 4000:
        review_reasons.append("oversized_message")

    if context is not None:
        if context.duplicate_text_count is not None and context.duplicate_text_count >= 3:
            review_reasons.append("duplicate_text_rate_hint")
        if context.recent_message_count is not None and context.recent_message_count >= 8:
            review_reasons.append("high_message_rate_hint")

    if delete_reasons:
        return ModerationDecision(
            ModerationAction.DELETE,
            tuple(dict.fromkeys(delete_reasons + review_reasons)),
        )
    if review_reasons:
        return ModerationDecision(
            ModerationAction.REVIEW,
            tuple(dict.fromkeys(review_reasons)),
        )
    return ModerationDecision(ModerationAction.KEEP, ("no_rule_match",))


def rule_based_moderation(text: str) -> ModerationAction:
    """Compatibility wrapper for callers that only need the action."""
    return evaluate_moderation(text).action
