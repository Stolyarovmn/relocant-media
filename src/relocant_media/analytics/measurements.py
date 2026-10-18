from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from relocant_media.services.analytics import DEFAULT_POST_SNAPSHOT_HOURS
from relocant_media.storage.models import (
    AIUsageEvent,
    AcquisitionCohort,
    ChannelSnapshot,
    PostSnapshot,
)


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_nonnegative(value: int, field: str) -> None:
    if value < 0:
        raise ValueError(f"{field} must be non-negative")


def record_post_snapshot(
    session: Session,
    *,
    post_id: int,
    captured_at: datetime,
    views: int,
    reactions: int,
    comments: int,
    forwards: int,
    subscriber_delta: int | None = None,
    horizon_hours: int | None = None,
) -> None:
    _require_aware(captured_at, "captured_at")
    for field, value in (
        ("views", views),
        ("reactions", reactions),
        ("comments", comments),
        ("forwards", forwards),
    ):
        _require_nonnegative(value, field)
    if horizon_hours is not None and horizon_hours not in DEFAULT_POST_SNAPSHOT_HOURS:
        raise ValueError("horizon_hours is not one of the configured snapshot horizons")

    statement = insert(PostSnapshot).values(
        post_id=post_id,
        captured_at=captured_at,
        horizon_hours=horizon_hours,
        views=views,
        reactions=reactions,
        comments=comments,
        forwards=forwards,
        subscriber_delta=subscriber_delta,
    )
    statement = statement.on_conflict_do_update(
        index_elements=["post_id", "captured_at"],
        set_={
            "horizon_hours": horizon_hours,
            "views": views,
            "reactions": reactions,
            "comments": comments,
            "forwards": forwards,
            "subscriber_delta": subscriber_delta,
        },
    )
    session.execute(statement)


def record_channel_snapshot(
    session: Session,
    *,
    city: str,
    captured_at: datetime,
    subscribers: int,
    source: str,
    kind: str = "observed",
    note: str | None = None,
) -> ChannelSnapshot:
    _require_aware(captured_at, "captured_at")
    _require_nonnegative(subscribers, "subscribers")
    if not city.strip() or not source.strip():
        raise ValueError("city and source are required")
    if kind not in {"observed", "correction"}:
        raise ValueError("channel snapshot kind must be observed or correction")
    snapshot = ChannelSnapshot(
        city=city,
        captured_at=captured_at,
        subscribers=subscribers,
        source=source,
        kind=kind,
        note=note,
    )
    session.add(snapshot)
    return snapshot


def upsert_acquisition_cohort(
    session: Session,
    *,
    city: str,
    campaign: str,
    cohort_date: date,
    joins: int,
    spend: Decimal | None = None,
    currency: str | None = None,
    retained_d1: int | None = None,
    retained_d7: int | None = None,
    retained_d30: int | None = None,
    source: str = "manual",
) -> None:
    _require_nonnegative(joins, "joins")
    if spend is not None and spend < 0:
        raise ValueError("spend must be non-negative")
    if (spend is None) != (currency is None):
        raise ValueError("spend and currency must either both be set or both be absent")
    for field, value in (
        ("retained_d1", retained_d1),
        ("retained_d7", retained_d7),
        ("retained_d30", retained_d30),
    ):
        if value is not None and (value < 0 or value > joins):
            raise ValueError(f"{field} must be between zero and joins")
    if not city.strip() or not campaign.strip() or not source.strip():
        raise ValueError("city, campaign and source are required")

    values = {
        "city": city,
        "campaign": campaign,
        "cohort_date": cohort_date,
        "spend": spend,
        "currency": currency.upper() if currency is not None else None,
        "joins": joins,
        "retained_d1": retained_d1,
        "retained_d7": retained_d7,
        "retained_d30": retained_d30,
        "source": source,
    }
    statement = insert(AcquisitionCohort).values(**values)
    statement = statement.on_conflict_do_update(
        constraint="uq_acquisition_city_campaign_date",
        set_={key: value for key, value in values.items() if key not in {"city", "campaign", "cohort_date"}},
    )
    session.execute(statement)


def record_ai_usage(
    session: Session,
    *,
    city: str,
    occurred_at: datetime,
    provider: str,
    model: str,
    task: str,
    status: str,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_amount: Decimal | None = None,
    currency: str | None = None,
    error_type: str | None = None,
) -> AIUsageEvent:
    _require_aware(occurred_at, "occurred_at")
    for field, value in (("input_tokens", input_tokens), ("output_tokens", output_tokens)):
        if value is not None:
            _require_nonnegative(value, field)
    if cost_amount is not None and cost_amount < 0:
        raise ValueError("cost_amount must be non-negative")
    if (cost_amount is None) != (currency is None):
        raise ValueError("cost_amount and currency must either both be set or both be absent")
    if not all(value.strip() for value in (city, provider, model, task, status)):
        raise ValueError("city, provider, model, task and status are required")

    event = AIUsageEvent(
        city=city,
        occurred_at=occurred_at,
        provider=provider,
        model=model,
        task=task,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_amount=cost_amount,
        currency=currency.upper() if currency is not None else None,
        error_type=error_type,
    )
    session.add(event)
    return event
