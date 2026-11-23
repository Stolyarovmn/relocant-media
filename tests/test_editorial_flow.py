import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ops import newsroom_export
from relocant_media.ops.daily_analysis import build_status, markdown
from relocant_media.ops.newsroom_merge import merge
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.editorial_flow import editorial_flow
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("Europe/Belgrade")
NOW = datetime(2026, 11, 22, 0, 30, tzinfo=UTC)
MIDNIGHT = datetime(2026, 11, 21, 23, tzinfo=UTC)
OFFICIAL = "city_beograd_news"
NATIONAL = "media_n1_srbija"


def source(source_id, ownership="official"):
    return Source(
        id=source_id, city="belgrade", name=source_id, url="https://example.rs/",
        source_type="official_city_news", trust=Decimal("0.8"), ownership=ownership,
    )


def item(session, source_id, published, ingested, precision="instant"):
    ident = str(session.query(RawItem).count() + 1)
    raw = RawItem(
        source_id=source_id, external_id=ident, url=f"https://example.rs/{ident}",
        title="Заголовок", body="Текст публичного материала", published_at=published,
        ingested_at=ingested, published_at_precision=precision,
    )
    session.add(raw)
    session.flush()
    return raw


def closed(session, raw, at, attempts=0):
    session.add(SchedulerRun(
        city="belgrade", task_type="material_lifecycle", task_key=f"material-lifecycle:{raw.id}",
        scheduled_for=at, started_at=at, finished_at=at, status="blocked",
        details={"raw_item_id": raw.id, "reason": "publication_older_than_seven_days",
                 "attempts": attempts, "fallback_attempts": 0, "audit_attempts": 0,
                 "private": "SECRET"},
    ))


def sent(session, raw, at, message_id, *, category="transport", status="sent", drafted=None):
    post = Post(
        city="belgrade", category=category, status="published", title="Опубликовано", body="Текст",
        confidence=Decimal("1"), published_at=at, telegram_message_id=message_id,
    )
    if drafted is not None:
        post.created_at = drafted
    session.add(post)
    session.flush()
    session.add(EditorialRun(
        raw_item_id=raw.id, post_id=post.id, outcome={}, evidence={}, delivery_status=status,
    ))


def test_expiry_uses_confirmed_closures_and_first_attempts_by_local_day():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    old = NOW - timedelta(days=8)
    with Session(engine) as session:
        session.add_all([source(OFFICIAL), source(NATIONAL, "media"), source("private", "reader")])
        session.flush()
        yesterday = item(session, OFFICIAL, old, old)
        today = item(session, NATIONAL, old, old)
        closed(session, yesterday, MIDNIGHT - timedelta(seconds=1))
        closed(session, today, MIDNIGHT)
        closed(session, today, MIDNIGHT + timedelta(minutes=1))  # Повтор одной записи.
        closed(session, item(session, NATIONAL, old, old), NOW, attempts=1)
        closed(session, item(session, NATIONAL, old, NOW - timedelta(minutes=2)), NOW)
        closed(session, item(session, "private", old, old), NOW)
        closed(session, item(session, OFFICIAL, old, old), NOW + timedelta(minutes=1))
        session.commit()
        report = editorial_flow(session, "belgrade", {OFFICIAL, NATIONAL, "private"},
                                now=NOW, zone=ZONE)

    assert len(report["days"]) == 8
    earlier, current = report["days"][-2:]
    assert earlier["local_date"] == "2026-11-21"
    assert current["local_date"] == "2026-11-22"
    assert earlier["expired_unreviewed"] == [{
        "source_id": OFFICIAL, "reason": "publication_older_than_seven_days", "count": 1,
    }]
    assert current["expired_unreviewed"] == [{
        "source_id": NATIONAL, "reason": "publication_older_than_seven_days", "count": 1,
    }]


def test_daily_report_reads_measured_latency_and_allowlists_aggregate_fields(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    class ReportClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(newsroom_export, "datetime", ReportClock)
    with Session(engine) as session:
        session.add_all([source(OFFICIAL), source(NATIONAL, "media")])
        session.flush()
        for number, delay in enumerate((12, 42), start=1):
            raw = item(session, OFFICIAL, NOW - timedelta(minutes=delay), MIDNIGHT)
            sent(session, raw, NOW, number)
        dated_only = item(session, NATIONAL, MIDNIGHT, MIDNIGHT, precision="day")
        sent(session, dated_only, NOW, 3)
        pending = item(session, NATIONAL, MIDNIGHT, MIDNIGHT)
        sent(session, pending, NOW, 4, status="pending")
        future = item(session, OFFICIAL, NOW + timedelta(minutes=5), MIDNIGHT)
        sent(session, future, NOW, 5, category="events")
        session.commit()
        saved = newsroom_export.snapshot(ROOT, session)

    flow = saved["cities"][0]["editorial_flow"]
    current = flow["days"][-1]
    current["private"] = "SECRET"
    current["expired_unreviewed"].append({
        "source_id": "PRIVATE_SOURCE", "reason": "event_expired", "count": 1,
    })
    saved = merge(newsroom_export.snapshot(ROOT), saved)
    daily = build_status(saved, city="belgrade", now=NOW)
    events, transport = daily["editorial_flow"]["days"][-1]["delivery_latency"]
    assert {key: events[key] for key in ("category", "samples", "missing_timestamp",
                                         "median_minutes")} == {
        "category": "events", "samples": 0, "missing_timestamp": 1, "median_minutes": None}
    assert {key: transport[key] for key in ("category", "samples", "missing_timestamp",
                                            "median_minutes")} == {
        "category": "transport", "samples": 2, "missing_timestamp": 1, "median_minutes": 27.0}
    assert "SECRET" not in json.dumps(daily)
    assert "PRIVATE_SOURCE" not in json.dumps(daily)
    assert "27.0 | 2 | 1" in markdown(daily)
    assert "2026-11-22 (неполный)" in markdown(daily)


def test_latency_splits_collection_review_and_release_stages():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([source(OFFICIAL), source(NATIONAL, "media")])
        session.flush()
        # Точное время: 10 мин сбор, 60 мин очередь и проверка, 20 мин ожидание выпуска.
        exact = item(session, NATIONAL, NOW - timedelta(minutes=90), NOW - timedelta(minutes=80))
        sent(session, exact, NOW, 1, drafted=NOW - timedelta(minutes=20))
        # Только дата: от источника не считается, этапы после сбора считаются.
        dated = item(session, OFFICIAL, MIDNIGHT, NOW - timedelta(minutes=30), precision="day")
        sent(session, dated, NOW, 2, drafted=NOW - timedelta(minutes=10))
        # Черновик переписан после выпуска: этапы проверки и выпуска не выдумываются.
        rewritten = item(session, OFFICIAL, MIDNIGHT, NOW - timedelta(minutes=50),
                         precision="day")
        sent(session, rewritten, NOW, 3, drafted=NOW + timedelta(minutes=5))
        # Отложенный ночным окном пост: ожидание выпуска видно отдельно.
        night = item(session, OFFICIAL, MIDNIGHT, MIDNIGHT, precision="day")
        sent(session, night, NOW, 4, category="city", drafted=MIDNIGHT + timedelta(minutes=5))
        session.commit()
        report = editorial_flow(session, "belgrade", {OFFICIAL, NATIONAL}, now=NOW, zone=ZONE)

    city, transport = report["days"][-1]["delivery_latency"]
    assert transport == {
        "category": "transport", "samples": 1, "missing_timestamp": 2, "median_minutes": 90.0,
        "seen_samples": 3, "seen_median_minutes": 50.0, "collection_median_minutes": 10.0,
        "review_median_minutes": 40.0, "release_median_minutes": 15.0,
    }
    assert city["seen_median_minutes"] == 90.0
    assert city["collection_median_minutes"] is None
    assert city["review_median_minutes"] == 5.0
    assert city["release_median_minutes"] == 85.0


def test_old_snapshots_without_stage_fields_stay_valid():
    old_flow = {"observed_at": NOW.isoformat(), "timezone": "Europe/Belgrade", "days": [{
        "local_date": "2026-11-22", "expired_unreviewed": [], "delivery_latency": [{
            "category": "transport", "samples": 0, "missing_timestamp": 2,
            "median_minutes": None}]}]}
    from relocant_media.services.editorial_flow import public_editorial_flow

    flow = public_editorial_flow(old_flow, set())
    assert flow is not None
    latency = flow["days"][0]["delivery_latency"][0]
    assert latency["seen_samples"] == 0
    assert latency["review_median_minutes"] is None
