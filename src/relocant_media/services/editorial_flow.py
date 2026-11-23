"""Агрегаты очереди и доставки из подтверждённого снимка."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, Field, ValidationError, field_validator
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from relocant_media.domain.freshness import evidence_freshness
from relocant_media.domain.topic_tags import TOPIC_CATEGORIES
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

_DAYS = 8
_PUBLIC = ("official", "media")
_CATEGORIES = TOPIC_CATEGORIES | {"other"}
ExpiryReason = Literal[
    "publication_older_than_seven_days", "publication_outside_news_window",
    "event_expired", "guide_verification_expired", "material_deadline_exceeded",
    "disruption_already_resolved",
]
_EXPIRY_REASONS: tuple[ExpiryReason, ...] = (
    "publication_older_than_seven_days", "publication_outside_news_window",
    "event_expired", "guide_verification_expired", "material_deadline_exceeded",
    "disruption_already_resolved",
)
Count = Annotated[int, Field(strict=True, ge=0)]
Minutes = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class Expiry(BaseModel):
    source_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")]
    reason: ExpiryReason
    count: Count


class Latency(BaseModel):
    category: str
    samples: Count
    missing_timestamp: Count
    median_minutes: Minutes | None
    # Этапы задержки (#435). Необязательные: старые снимки остаются валидными.
    seen_samples: Count = 0
    seen_median_minutes: Minutes | None = None
    collection_median_minutes: Minutes | None = None
    review_median_minutes: Minutes | None = None
    release_median_minutes: Minutes | None = None

    @field_validator("category")
    @classmethod
    def known_category(cls, value: str) -> str:
        if value not in _CATEGORIES:
            raise ValueError("Неизвестная рубрика в отчёте")
        return value


class Day(BaseModel):
    local_date: date
    expired_unreviewed: list[Expiry]
    delivery_latency: list[Latency]


class EditorialFlow(BaseModel):
    observed_at: AwareDatetime
    timezone: str
    days: Annotated[list[Day], Field(max_length=_DAYS)]

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Неизвестная часовая зона") from None
        return value


def public_editorial_flow(value: object, source_ids: set[str]) -> dict[str, Any] | None:
    try:
        flow = EditorialFlow.model_validate(value)
    except ValidationError:
        return None
    for day in flow.days:
        day.expired_unreviewed = [
            entry for entry in day.expired_unreviewed if entry.source_id in source_ids
        ]
    return flow.model_dump(mode="json")


def editorial_flow(
    session: Session, city: str, source_ids: set[str], *, now: datetime, zone: ZoneInfo,
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("Для отчёта нужна часовая зона")
    local_today = now.astimezone(zone).date()
    first_day = local_today - timedelta(days=_DAYS - 1)
    start = datetime.combine(first_day, time.min, zone)
    public_sources = and_(Source.city == city, Source.ownership.in_(_PUBLIC),
                          Source.id.in_(source_ids))
    expiry: Counter[tuple[date, str, ExpiryReason]] = Counter()
    counted = set()
    details = SchedulerRun.details
    expired = session.execute(
        select(RawItem, Source.source_type, SchedulerRun.finished_at,
               details["reason"].as_string())
        .select_from(SchedulerRun)
        .join(RawItem, RawItem.id == details["raw_item_id"].as_integer())
        .join(Source, Source.id == RawItem.source_id)
        .outerjoin(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .where(
            public_sources, SchedulerRun.city == city,
            SchedulerRun.task_type == "material_lifecycle",
            SchedulerRun.status == "blocked", SchedulerRun.finished_at.between(start, now),
            details["reason"].as_string().in_(_EXPIRY_REASONS),
            details["attempts"].as_integer() == 0, details["fallback_attempts"].as_integer() == 0,
            details["audit_attempts"].as_integer() == 0,
            details["lifecycle_root_raw_id"].as_integer().is_(None),
            details["replacement_raw_item_id"].as_integer().is_(None),
            EditorialRun.raw_item_id.is_(None),
        )
        .order_by(SchedulerRun.finished_at, SchedulerRun.id)
    )
    for row in expired:
        raw, source_type, closed_at, reason = cast(
            tuple[RawItem, str, datetime | None, ExpiryReason], row,
        )
        if closed_at is None or raw.id in counted or raw.ingested_at >= closed_at:
            continue
        try:
            arrival = evidence_freshness(
                raw.title, raw.body, raw.published_at, raw.ingested_at,
                precision=raw.published_at_precision, source_type=source_type,
                verified_at=raw.ingested_at, timezone_name=zone.key,
            )
        except ValueError:
            continue
        if arrival.action != "keep":
            continue
        counted.add(raw.id)
        expiry[closed_at.astimezone(zone).date(), raw.source_id, reason] += 1

    measured: dict[tuple[date, str], list[float]] = defaultdict(list)
    missing: Counter[tuple[date, str]] = Counter()
    stages: dict[tuple[date, str], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list))
    delivered = session.execute(
        select(
            Post.published_at, Post.category, RawItem.published_at, RawItem.published_at_precision,
            RawItem.ingested_at, Post.created_at,
        )
        .join(EditorialRun, EditorialRun.post_id == Post.id)
        .join(RawItem, RawItem.id == EditorialRun.raw_item_id)
        .join(Source, Source.id == RawItem.source_id)
        .where(public_sources, Post.city == city, EditorialRun.delivery_status == "sent",
               Post.telegram_message_id > 0, Post.published_at.between(start, now))
    )
    for sent_at, category, source_time, precision, ingested_at, drafted_at in delivered:
        if sent_at is None:
            continue
        key = sent_at.astimezone(zone).date(), category if category in _CATEGORIES else "other"
        exact: datetime | None = source_time if (
            precision == "instant" and source_time is not None and source_time <= sent_at
        ) else None
        if exact is not None:
            measured[key].append(_minutes(exact, sent_at))
        else:
            missing[key] += 1
        bucket = stages[key]
        seen, drafted = ingested_at, drafted_at
        if seen is not None and seen <= sent_at:
            bucket["seen"].append(_minutes(seen, sent_at))
            if exact is not None and exact <= seen:
                bucket["collection"].append(_minutes(exact, seen))
            if drafted is not None and seen <= drafted <= sent_at:
                bucket["review"].append(_minutes(seen, drafted))
                bucket["release"].append(_minutes(drafted, sent_at))

    latency_by_day: dict[date, list[Latency]] = defaultdict(list)
    for key in sorted(measured.keys() | missing.keys()):
        observed, category = key
        samples = measured.get(key, [])
        bucket = stages.get(key, {})
        latency_by_day[observed].append(Latency(
            category=category, samples=len(samples), missing_timestamp=missing[key],
            median_minutes=_median(samples),
            seen_samples=len(bucket.get("seen", [])),
            seen_median_minutes=_median(bucket.get("seen", [])),
            collection_median_minutes=_median(bucket.get("collection", [])),
            review_median_minutes=_median(bucket.get("review", [])),
            release_median_minutes=_median(bucket.get("release", [])),
        ))

    days = []
    for offset in range(_DAYS):
        day = first_day + timedelta(days=offset)
        days.append(Day(
            local_date=day,
            expired_unreviewed=[
                Expiry(source_id=source_id, reason=reason, count=count)
                for (observed, source_id, reason), count in sorted(expiry.items())
                if observed == day
            ],
            delivery_latency=latency_by_day[day],
        ))
    return EditorialFlow(observed_at=now, timezone=zone.key, days=days).model_dump(mode="json")


def _minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60


def _median(values: list[float]) -> float | None:
    return round(median(values), 1) if values else None
