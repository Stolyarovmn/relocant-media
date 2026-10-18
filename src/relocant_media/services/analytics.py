from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

DEFAULT_POST_SNAPSHOT_HOURS: tuple[int, ...] = (1, 6, 24, 72)


@dataclass(frozen=True, slots=True)
class PostSnapshot:
    views: int
    reactions: int
    comments: int
    forwards: int


class RecommendationClass(StrEnum):
    AUTO = "AUTO"
    PR_REVIEW = "PR_REVIEW"
    OWNER_APPROVAL = "OWNER_APPROVAL"


@dataclass(frozen=True, slots=True)
class Recommendation:
    action_class: RecommendationClass
    code: str
    message: str


def engagement_per_view(snapshot: PostSnapshot) -> float:
    if snapshot.views <= 0:
        return 0.0
    interactions = snapshot.reactions + snapshot.comments + snapshot.forwards
    return interactions / snapshot.views


def effective_cpl(spend_eur: float, retained_subscribers: int) -> float | None:
    if retained_subscribers <= 0:
        return None
    return spend_eur / retained_subscribers


def retained_cpl(spend: Decimal, retained_subscribers: int) -> Decimal | None:
    if spend < 0:
        raise ValueError("spend must be non-negative")
    if retained_subscribers < 0:
        raise ValueError("retained subscriber count must be non-negative")
    if retained_subscribers == 0:
        return None
    return spend / Decimal(retained_subscribers)


def planned_snapshot_times(
    published_at: datetime,
    horizons: tuple[int, ...] = DEFAULT_POST_SNAPSHOT_HOURS,
) -> tuple[tuple[int, datetime], ...]:
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("published_at must be timezone-aware")
    if not horizons or any(hours <= 0 for hours in horizons):
        raise ValueError("snapshot horizons must be positive")
    if tuple(sorted(set(horizons))) != horizons:
        raise ValueError("snapshot horizons must be sorted and unique")
    return tuple((hours, published_at + timedelta(hours=hours)) for hours in horizons)
