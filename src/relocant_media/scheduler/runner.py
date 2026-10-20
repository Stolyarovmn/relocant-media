from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.analytics.measurements import record_channel_snapshot, record_post_snapshot
from relocant_media.analytics.report import build_daily_report, store_daily_report
from relocant_media.collectors.factory import build_collector
from relocant_media.ingestion.persistence import (
    capture_collected_batch,
    record_collector_failure,
    source_is_enabled,
    sync_source,
)
from relocant_media.scheduler.config import CityOperationsConfig
from relocant_media.scheduler.metrics import MetricsProvider, MetricsProviderUnavailable
from relocant_media.scheduler.state import (
    claim_lease,
    finish_run,
    latest_run,
    release_lease,
    retry_is_due,
    start_run,
)
from relocant_media.scheduler.timing import bucket_start, parse_interval
from relocant_media.services.analytics import DEFAULT_POST_SNAPSHOT_HOURS, planned_snapshot_times
from relocant_media.sources.registry import SourceConfig, load_source_registry
from relocant_media.storage.models import DailyReport, Post, PostSnapshot


@dataclass(slots=True)
class SchedulerSummary:
    source_runs: int = 0
    source_failures: int = 0
    source_locked: int = 0
    source_review_required: int = 0
    channel_snapshots_recorded: int = 0
    channel_snapshots_blocked: int = 0
    channel_snapshot_failures: int = 0
    snapshots_recorded: int = 0
    snapshots_blocked: int = 0
    snapshot_failures: int = 0
    reports_stored: int = 0
    report_failures: int = 0


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler timestamp must be timezone-aware")


def _recurring_task_due(
    session: Session,
    *,
    task_key: str,
    interval: timedelta,
    now: datetime,
) -> bool:
    run = latest_run(session, task_key)
    if run is None:
        return True
    if run.status == "success":
        finished = run.finished_at or run.started_at
        return finished + interval <= now
    return retry_is_due(run, now)


def _one_shot_task_due(session: Session, *, task_key: str, now: datetime) -> bool:
    run = latest_run(session, task_key)
    if run is None:
        return True
    if run.status == "success":
        return False
    return retry_is_due(run, now)


async def _run_source(
    session: Session,
    client: httpx.AsyncClient,
    *,
    city: str,
    source: SourceConfig,
    now: datetime,
    summary: SchedulerSummary,
) -> None:
    interval = parse_interval(source.check_every)
    task_key = f"source:{city}:{source.id}"
    if not _recurring_task_due(session, task_key=task_key, interval=interval, now=now):
        return
    scheduled_for = bucket_start(now, interval)
    if not claim_lease(session, task_key=task_key, now=now):
        summary.source_locked += 1
        return

    run_id = start_run(
        session,
        task_key=task_key,
        task_type="source_ingestion",
        city=city,
        scheduled_for=scheduled_for,
        now=now,
    )
    try:
        collector = build_collector(source, client)
        items = await collector.collect()
        written, changes = capture_collected_batch(session, items)
        session.commit()
        finish_run(
            session,
            run_id=run_id,
            status="success",
            now=now,
            details={"source_id": source.id, "raw_items_written": written,
                     "review_required": changes},
        )
        summary.source_runs += 1
        summary.source_review_required += changes
    except Exception as error:
        session.rollback()
        record_collector_failure(session, source, error)
        session.commit()
        finish_run(session, run_id=run_id, status="failed", now=now, error=error)
        summary.source_failures += 1
    finally:
        release_lease(session, task_key=task_key, now=now)


async def schedule_sources(
    session: Session,
    client: httpx.AsyncClient,
    *,
    city: str,
    source_directory: Path,
    now: datetime,
    summary: SchedulerSummary,
) -> None:
    sources = load_source_registry(source_directory)
    for source in sources:
        sync_source(session, city, source)
    session.commit()

    for source in sources:
        if source_is_enabled(source):
            await _run_source(
                session,
                client,
                city=city,
                source=source,
                now=now,
                summary=summary,
            )


async def schedule_channel_snapshot(
    session: Session,
    *,
    city_config: CityOperationsConfig,
    now: datetime,
    provider: MetricsProvider,
    summary: SchedulerSummary,
) -> None:
    interval = parse_interval(city_config.operations.channel_snapshot_every)
    task_key = f"channel_snapshot:{city_config.id}"
    if not _recurring_task_due(session, task_key=task_key, interval=interval, now=now):
        return
    scheduled_for = bucket_start(now, interval)
    if not claim_lease(session, task_key=task_key, now=now):
        return

    run_id = start_run(
        session,
        task_key=task_key,
        task_type="channel_snapshot",
        city=city_config.id,
        scheduled_for=scheduled_for,
        now=now,
    )
    try:
        metrics = await provider.fetch_channel_metrics()
        record_channel_snapshot(
            session,
            city=city_config.id,
            captured_at=now,
            subscribers=metrics.subscribers,
            source=metrics.source,
        )
        session.commit()
        finish_run(
            session,
            run_id=run_id,
            status="success",
            now=now,
            details={"subscribers": metrics.subscribers, "source": metrics.source},
        )
        summary.channel_snapshots_recorded += 1
    except MetricsProviderUnavailable as error:
        session.rollback()
        finish_run(session, run_id=run_id, status="blocked", now=now, error=error)
        summary.channel_snapshots_blocked += 1
    except Exception as error:
        session.rollback()
        finish_run(session, run_id=run_id, status="failed", now=now, error=error)
        summary.channel_snapshot_failures += 1
    finally:
        release_lease(session, task_key=task_key, now=now)


async def schedule_post_snapshots(
    session: Session,
    *,
    city: str,
    now: datetime,
    provider: MetricsProvider,
    summary: SchedulerSummary,
) -> None:
    posts = list(
        session.scalars(
            select(Post).where(
                Post.city == city,
                Post.published_at.is_not(None),
                Post.telegram_message_id.is_not(None),
            )
        )
    )
    for post in posts:
        published_at = post.published_at
        if published_at is None:
            continue
        for horizon, due_at in planned_snapshot_times(
            published_at, DEFAULT_POST_SNAPSHOT_HOURS
        ):
            if due_at > now:
                continue
            exists = session.scalar(
                select(PostSnapshot.post_id).where(
                    PostSnapshot.post_id == post.id,
                    PostSnapshot.horizon_hours == horizon,
                )
            )
            if exists is not None:
                continue

            task_key = f"snapshot:{city}:{post.id}:{horizon}h"
            if not _one_shot_task_due(session, task_key=task_key, now=now):
                continue
            if not claim_lease(session, task_key=task_key, now=now):
                continue
            run_id = start_run(
                session,
                task_key=task_key,
                task_type="post_snapshot",
                city=city,
                scheduled_for=due_at,
                now=now,
            )
            try:
                metrics = await provider.fetch_post_metrics(post)
                record_post_snapshot(
                    session,
                    post_id=post.id,
                    captured_at=now,
                    horizon_hours=horizon,
                    views=metrics.views,
                    reactions=metrics.reactions,
                    comments=metrics.comments,
                    forwards=metrics.forwards,
                    subscriber_delta=metrics.subscriber_delta,
                )
                session.commit()
                finish_run(
                    session,
                    run_id=run_id,
                    status="success",
                    now=now,
                    details={"post_id": post.id, "horizon_hours": horizon},
                )
                summary.snapshots_recorded += 1
            except MetricsProviderUnavailable as error:
                session.rollback()
                finish_run(session, run_id=run_id, status="blocked", now=now, error=error)
                summary.snapshots_blocked += 1
            except Exception as error:
                session.rollback()
                finish_run(session, run_id=run_id, status="failed", now=now, error=error)
                summary.snapshot_failures += 1
            finally:
                release_lease(session, task_key=task_key, now=now)


def _latest_report_date(session: Session, city: str) -> date | None:
    return session.scalar(
        select(DailyReport.report_date)
        .where(DailyReport.city == city)
        .order_by(DailyReport.report_date.desc())
        .limit(1)
    )


def _next_report_date(
    session: Session,
    *,
    city_config: CityOperationsConfig,
    now: datetime,
) -> date | None:
    zone = ZoneInfo(city_config.timezone)
    local_now = now.astimezone(zone)
    report_time = city_config.operations.daily_report_local_time
    latest_due = local_now.date() - timedelta(days=1)
    if local_now.time().replace(tzinfo=None) < report_time:
        latest_due -= timedelta(days=1)

    last_report = _latest_report_date(session, city_config.id)
    if last_report is None:
        return latest_due
    candidate = last_report + timedelta(days=1)
    return candidate if candidate <= latest_due else None


def schedule_daily_report(
    session: Session,
    *,
    city_config: CityOperationsConfig,
    now: datetime,
    summary: SchedulerSummary,
) -> None:
    report_date = _next_report_date(session, city_config=city_config, now=now)
    if report_date is None:
        return

    zone = ZoneInfo(city_config.timezone)
    scheduled_for = datetime.combine(
        report_date + timedelta(days=1),
        city_config.operations.daily_report_local_time,
        tzinfo=zone,
    )
    task_key = f"daily_report:{city_config.id}:{report_date.isoformat()}"
    if not _one_shot_task_due(session, task_key=task_key, now=now):
        return
    if not claim_lease(session, task_key=task_key, now=now):
        return
    run_id = start_run(
        session,
        task_key=task_key,
        task_type="daily_report",
        city=city_config.id,
        scheduled_for=scheduled_for,
        now=now,
    )
    try:
        payload = build_daily_report(
            session,
            report_date=report_date,
            city=city_config.id,
            timezone_name=city_config.timezone,
        )
        store_daily_report(
            session,
            report_date=report_date,
            city=city_config.id,
            payload=payload,
        )
        session.commit()
        finish_run(
            session,
            run_id=run_id,
            status="success",
            now=now,
            details={"report_date": report_date.isoformat()},
        )
        summary.reports_stored += 1
    except Exception as error:
        session.rollback()
        finish_run(session, run_id=run_id, status="failed", now=now, error=error)
        summary.report_failures += 1
    finally:
        release_lease(session, task_key=task_key, now=now)


async def run_scheduler_tick(
    session: Session,
    client: httpx.AsyncClient,
    *,
    city_config: CityOperationsConfig,
    source_directory: Path,
    now: datetime,
    metrics_provider: MetricsProvider,
) -> SchedulerSummary:
    _require_aware(now)
    summary = SchedulerSummary()
    await schedule_sources(
        session,
        client,
        city=city_config.id,
        source_directory=source_directory,
        now=now,
        summary=summary,
    )
    await schedule_channel_snapshot(
        session,
        city_config=city_config,
        now=now,
        provider=metrics_provider,
        summary=summary,
    )
    await schedule_post_snapshots(
        session,
        city=city_config.id,
        now=now,
        provider=metrics_provider,
        summary=summary,
    )
    schedule_daily_report(session, city_config=city_config, now=now, summary=summary)
    return summary
