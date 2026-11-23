"""Агрегаты очереди и доставки из подтверждённого снимка."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from statistics import median
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, Field, ValidationError, field_validator
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from relocant_media.domain.freshness import evidence_freshness
from relocant_media.domain.quiet_hours import QUIET_END, QUIET_START
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
_REPORT_DAY_END = time(21)


class Expiry(BaseModel):
    source_id: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,96}$")]
    reason: ExpiryReason
    count: Count


class Timing(BaseModel):
    samples: Count
    median_minutes: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None


class LatencyStages(BaseModel):
    collection: Timing
    before_post_record: Timing
    after_post_record: Timing
    since_ingestion: Timing


class Latency(Timing):
    category: str
    missing_timestamp: Count
    stages: LatencyStages | None = None

    @field_validator("category")
    @classmethod
    def known_category(cls, value: str) -> str:
        if value not in _CATEGORIES:
            raise ValueError("Неизвестная рубрика в отчёте")
        return value


class PublicationCadence(BaseModel):
    posts: Count
    quiet_hour_posts: Count
    max_daytime_gap_minutes: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None


class Day(BaseModel):
    local_date: date
    expired_unreviewed: list[Expiry]
    delivery_latency: list[Latency]
    publication_cadence: PublicationCadence | None = None


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
    return flow.model_dump(mode="json", exclude_unset=True)


def _timing(samples: list[float]) -> Timing:
    return Timing(
        samples=len(samples), median_minutes=round(median(samples), 1) if samples else None,
    )


def _record_interval(samples: list[float], start: datetime | None, end: datetime) -> None:
    if start is not None and start <= end:
        samples.append((end - start).total_seconds() / 60)


def _publication_cadence(
    day: date, delivered_at: list[datetime], now: datetime, zone: ZoneInfo,
) -> PublicationCadence:
    opening = datetime.combine(day, QUIET_END, zone)
    closing = min(datetime.combine(day, _REPORT_DAY_END, zone), now)
    gap = None
    if closing > opening:
        marks = [opening, *sorted(sent for sent in delivered_at if opening < sent < closing),
                 closing]
        gap = round(max((right - left).total_seconds() / 60 for left, right in pairwise(marks)), 1)
    return PublicationCadence(
        posts=len(delivered_at),
        quiet_hour_posts=sum(
            sent.time() < QUIET_END or sent.time() >= QUIET_START for sent in delivered_at
        ),
        max_daytime_gap_minutes=gap,
    )


@dataclass
class _DeliveryTimes:
    total: list[float] = field(default_factory=list)
    collection: list[float] = field(default_factory=list)
    before_post_record: list[float] = field(default_factory=list)
    after_post_record: list[float] = field(default_factory=list)
    since_ingestion: list[float] = field(default_factory=list)
    missing_timestamp: int = 0

    def summary(self, category: str) -> Latency:
        total = _timing(self.total)
        return Latency(
            category=category, samples=total.samples, median_minutes=total.median_minutes,
            missing_timestamp=self.missing_timestamp,
            stages=LatencyStages(
                collection=_timing(self.collection),
                before_post_record=_timing(self.before_post_record),
                after_post_record=_timing(self.after_post_record),
                since_ingestion=_timing(self.since_ingestion),
            ),
        )


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

    measured: dict[tuple[date, str], _DeliveryTimes] = defaultdict(_DeliveryTimes)
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
    for sent_at, category, source_time, precision, ingested_at, created_at in delivered:
        if sent_at is None:
            continue
        key = sent_at.astimezone(zone).date(), category if category in _CATEGORIES else "other"
        times = measured[key]
        exact_time = source_time if precision == "instant" else None
        if exact_time is None or exact_time > sent_at:
            times.missing_timestamp += 1
        else:
            _record_interval(times.total, exact_time, sent_at)
        if ingested_at is None or ingested_at > sent_at:
            continue
        _record_interval(times.collection, exact_time, ingested_at)
        _record_interval(times.since_ingestion, ingested_at, sent_at)
        if created_at is not None and ingested_at <= created_at <= sent_at:
            _record_interval(times.before_post_record, ingested_at, created_at)
            _record_interval(times.after_post_record, created_at, sent_at)

    latency_by_day: dict[date, list[Latency]] = defaultdict(list)
    for (observed, category), times in sorted(measured.items()):
        latency_by_day[observed].append(times.summary(category))

    delivered_by_day: dict[date, list[datetime]] = defaultdict(list)
    receipts = session.execute(
        select(Post.telegram_message_id, Post.published_at)
        .where(
            Post.city == city, Post.telegram_message_id > 0, Post.published_at.between(start, now),
            select(EditorialRun.raw_item_id).where(
                EditorialRun.post_id == Post.id, EditorialRun.delivery_status == "sent",
            ).exists(),
        )
        .order_by(Post.published_at, Post.id)
    )
    seen_messages: set[int] = set()
    for message_id, sent_at in receipts:
        if message_id is None or sent_at is None or message_id in seen_messages:
            continue
        seen_messages.add(message_id)
        local = sent_at.astimezone(zone)
        delivered_by_day[local.date()].append(local)

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
            publication_cadence=_publication_cadence(day, delivered_by_day[day], now, zone),
        ))
    return EditorialFlow(observed_at=now, timezone=zone.key, days=days).model_dump(mode="json")
