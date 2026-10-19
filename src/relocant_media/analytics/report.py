from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from relocant_media.services.analytics import Recommendation, RecommendationClass, retained_cpl
from relocant_media.storage.models import (
    AcquisitionCohort,
    AIUsageEvent,
    ChannelSnapshot,
    CollectorFailure,
    DailyReport,
    ModerationAction,
    Post,
    PostSnapshot,
    RawItem,
    Source,
)


def _window(report_date: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    start = datetime.combine(report_date, time.min, tzinfo=zone)
    return start, start + timedelta(days=1)


def _money(value: Decimal) -> str:
    return format(value, "f")


def _retention_cpl(
    cohorts: list[AcquisitionCohort],
    report_date: date,
    days: int,
    field: str,
) -> dict[str, dict[str, str | int | None]]:
    spend_by_currency: defaultdict[str, Decimal] = defaultdict(Decimal)
    retained_by_currency: Counter[str] = Counter()
    cohorts_by_currency: Counter[str] = Counter()
    cutoff = report_date - timedelta(days=days)

    for cohort in cohorts:
        if cohort.cohort_date > cutoff or cohort.spend is None or cohort.currency is None:
            continue
        retained = getattr(cohort, field)
        if not isinstance(retained, int):
            continue
        spend_by_currency[cohort.currency] += cohort.spend
        retained_by_currency[cohort.currency] += retained
        cohorts_by_currency[cohort.currency] += 1

    result: dict[str, dict[str, str | int | None]] = {}
    for currency, spend in sorted(spend_by_currency.items()):
        retained = retained_by_currency[currency]
        cpl = retained_cpl(spend, retained)
        result[currency] = {
            "cohorts": cohorts_by_currency[currency],
            "spend": _money(spend),
            "retained": retained,
            "cpl": _money(cpl) if cpl is not None else None,
        }
    return result


def _recommendations(
    *,
    source_failures: int,
    ai_errors: int,
    audience_snapshots: int,
) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    if source_failures:
        recommendations.append(
            Recommendation(
                RecommendationClass.PR_REVIEW,
                "source_failures",
                "Inspect source/collector failures before changing content automation.",
            )
        )
    if ai_errors:
        recommendations.append(
            Recommendation(
                RecommendationClass.PR_REVIEW,
                "ai_errors",
                "Inspect AI errors; do not relax verification gates automatically.",
            )
        )
    if audience_snapshots < 2:
        recommendations.append(
            Recommendation(
                RecommendationClass.AUTO,
                "collect_audience_samples",
                "Continue collecting audience snapshots; one point is insufficient for a trend.",
            )
        )
    return recommendations


def _performance_by_rule_version(
    session: Session,
    snapshots: list[PostSnapshot],
) -> dict[str, dict[str, int | float | None]]:
    totals: dict[str, dict[str, int]] = defaultdict(
        lambda: {"snapshots": 0, "views": 0, "interactions": 0}
    )
    for snapshot in snapshots:
        post = session.get(Post, snapshot.post_id)
        if post is None:
            continue
        version = post.editorial_rule_version
        item = totals[version]
        item["snapshots"] += 1
        item["views"] += snapshot.views
        item["interactions"] += snapshot.reactions + snapshot.comments + snapshot.forwards

    result: dict[str, dict[str, int | float | None]] = {}
    for version, item in sorted(totals.items()):
        views = item["views"]
        result[version] = {
            **item,
            "engagement_per_view_observed": item["interactions"] / views if views else None,
        }
    return result


def build_daily_report(
    session: Session,
    *,
    report_date: date,
    city: str,
    timezone_name: str,
) -> dict[str, Any]:
    start, end = _window(report_date, timezone_name)

    posts = list(
        session.scalars(
            select(Post).where(
                Post.city == city,
                Post.published_at >= start,
                Post.published_at < end,
            )
        )
    )
    snapshots = list(
        session.scalars(
            select(PostSnapshot)
            .join(Post, Post.id == PostSnapshot.post_id)
            .where(
                Post.city == city,
                PostSnapshot.captured_at >= start,
                PostSnapshot.captured_at < end,
            )
        )
    )
    total_views = sum(snapshot.views for snapshot in snapshots)
    total_interactions = sum(
        snapshot.reactions + snapshot.comments + snapshot.forwards for snapshot in snapshots
    )
    published_by_version = Counter(post.editorial_rule_version for post in posts)
    performance_by_version = _performance_by_rule_version(session, snapshots)

    audience = list(
        session.scalars(
            select(ChannelSnapshot)
            .where(
                ChannelSnapshot.city == city,
                ChannelSnapshot.captured_at >= start,
                ChannelSnapshot.captured_at < end,
            )
            .order_by(ChannelSnapshot.captured_at)
        )
    )
    audience_delta = None
    if len(audience) >= 2:
        audience_delta = audience[-1].subscribers - audience[0].subscribers

    moderation = list(
        session.scalars(
            select(ModerationAction).where(
                ModerationAction.city == city,
                ModerationAction.created_at >= start,
                ModerationAction.created_at < end,
            )
        )
    )
    moderation_counts = Counter(action.action for action in moderation)

    raw_items = list(
        session.scalars(
            select(RawItem)
            .join(Source, Source.id == RawItem.source_id)
            .where(
                Source.city == city,
                RawItem.ingested_at >= start,
                RawItem.ingested_at < end,
            )
        )
    )
    failures = list(
        session.scalars(
            select(CollectorFailure)
            .join(Source, Source.id == CollectorFailure.source_id)
            .where(
                Source.city == city,
                CollectorFailure.occurred_at >= start,
                CollectorFailure.occurred_at < end,
            )
        )
    )

    ai_events = list(
        session.scalars(
            select(AIUsageEvent).where(
                AIUsageEvent.city == city,
                AIUsageEvent.occurred_at >= start,
                AIUsageEvent.occurred_at < end,
            )
        )
    )
    ai_errors = sum(event.status != "ok" for event in ai_events)
    token_complete = all(
        event.input_tokens is not None and event.output_tokens is not None for event in ai_events
    )
    input_tokens = sum(event.input_tokens or 0 for event in ai_events)
    output_tokens = sum(event.output_tokens or 0 for event in ai_events)
    ai_costs: defaultdict[str, Decimal] = defaultdict(Decimal)
    cost_events = 0
    for event in ai_events:
        if event.cost_amount is not None and event.currency is not None:
            ai_costs[event.currency] += event.cost_amount
            cost_events += 1

    all_cohorts = list(
        session.scalars(
            select(AcquisitionCohort).where(
                AcquisitionCohort.city == city,
                AcquisitionCohort.cohort_date <= report_date,
            )
        )
    )
    today_cohorts = [cohort for cohort in all_cohorts if cohort.cohort_date == report_date]
    spend_today: defaultdict[str, Decimal] = defaultdict(Decimal)
    joins_today = sum(cohort.joins for cohort in today_cohorts)
    for cohort in today_cohorts:
        if cohort.spend is not None and cohort.currency is not None:
            spend_today[cohort.currency] += cohort.spend

    recommendations = _recommendations(
        source_failures=len(failures),
        ai_errors=ai_errors,
        audience_snapshots=len(audience),
    )

    return {
        "report_date": report_date.isoformat(),
        "city": city,
        "timezone": timezone_name,
        "content": {
            "published_posts": len(posts),
            "published_posts_by_editorial_rule_version": dict(sorted(published_by_version.items())),
            "post_snapshots": len(snapshots),
            "views_observed": total_views,
            "interactions_observed": total_interactions,
            "engagement_per_view_observed": (
                total_interactions / total_views if total_views > 0 else None
            ),
            "performance_by_editorial_rule_version": performance_by_version,
        },
        "audience": {
            "snapshots": len(audience),
            "first_subscribers": audience[0].subscribers if audience else None,
            "last_subscribers": audience[-1].subscribers if audience else None,
            "observed_delta": audience_delta,
        },
        "moderation": {
            "actions": len(moderation),
            "by_action": dict(sorted(moderation_counts.items())),
        },
        "sources": {
            "raw_items_ingested": len(raw_items),
            "collector_failures": len(failures),
        },
        "ai": {
            "events": len(ai_events),
            "errors": ai_errors,
            "input_tokens_observed": input_tokens,
            "output_tokens_observed": output_tokens,
            "token_coverage_complete": token_complete,
            "cost_events": cost_events,
            "costs_observed": {key: _money(value) for key, value in sorted(ai_costs.items())},
        },
        "acquisition": {
            "cohorts_started": len(today_cohorts),
            "joins_observed": joins_today,
            "spend_observed": {
                key: _money(value) for key, value in sorted(spend_today.items())
            },
            "retained_cpl_d1": _retention_cpl(all_cohorts, report_date, 1, "retained_d1"),
            "retained_cpl_d7": _retention_cpl(all_cohorts, report_date, 7, "retained_d7"),
            "retained_cpl_d30": _retention_cpl(
                all_cohorts,
                report_date,
                30,
                "retained_d30",
            ),
        },
        "recommendations": [
            {
                "class": recommendation.action_class.value,
                "code": recommendation.code,
                "message": recommendation.message,
            }
            for recommendation in recommendations
        ],
        "limitations": [
            "Metrics are observational; this report does not infer causality.",
            (
                "Editorial rule versions are compared descriptively; version labels "
                "do not prove causality."
            ),
            "Currencies are never converted or combined across currencies.",
            "AI cost is reported only when an explicit observed cost was recorded.",
            "Budget changes require an explicit owner-defined policy and OWNER_APPROVAL.",
        ],
    }


def store_daily_report(
    session: Session,
    *,
    report_date: date,
    city: str,
    payload: dict[str, Any],
) -> None:
    statement = insert(DailyReport).values(report_date=report_date, city=city, payload=payload)
    statement = statement.on_conflict_do_update(
        index_elements=["report_date", "city"],
        set_={"payload": payload},
    )
    session.execute(statement)
