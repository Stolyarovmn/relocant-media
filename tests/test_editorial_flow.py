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
from relocant_media.services.editorial_flow import editorial_flow, public_editorial_flow
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


def sent(session, raw, at, message_id, *, category="transport", status="sent",
         created_at=None, outcome=None):
    post = Post(
        city="belgrade", category=category, status="published", title="Опубликовано", body="Текст",
        confidence=Decimal("1"), published_at=at, telegram_message_id=message_id,
        created_at=created_at,
    )
    session.add(post)
    session.flush()
    session.add(EditorialRun(
        raw_item_id=raw.id, post_id=post.id, outcome=outcome or {}, evidence={},
        delivery_status=status,
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
            raw = item(
                session, OFFICIAL, NOW - timedelta(minutes=delay), NOW - timedelta(minutes=10),
            )
            sent(session, raw, NOW, number, created_at=NOW - timedelta(minutes=4))
        dated_only = item(session, NATIONAL, MIDNIGHT, MIDNIGHT, precision="day")
        sent(session, dated_only, NOW, 3, created_at=MIDNIGHT + timedelta(minutes=5),
             outcome={"_package": {"editor_model": "gpt"}})
        pending = item(session, NATIONAL, MIDNIGHT, MIDNIGHT)
        sent(session, pending, NOW, 4, status="pending")
        future = item(session, OFFICIAL, NOW + timedelta(minutes=5), MIDNIGHT)
        sent(session, future, NOW, 5, category="events", created_at=MIDNIGHT - timedelta(minutes=1))
        session.commit()
        saved = newsroom_export.snapshot(ROOT, session)

    flow = saved["cities"][0]["editorial_flow"]
    current = flow["days"][-1]
    current["private"] = "SECRET"
    current["publication_cadence"]["private"] = "SECRET"
    current["expired_unreviewed"].append({
        "source_id": "PRIVATE_SOURCE", "reason": "event_expired", "count": 1,
    })
    saved = merge(newsroom_export.snapshot(ROOT), saved)
    daily = build_status(saved, city="belgrade", now=NOW)
    rows = daily["editorial_flow"]["days"][-1]["delivery_latency"]
    assert [{key: value for key, value in row.items() if key != "stages"} for row in rows] == [
        {"category": "events", "samples": 0, "missing_timestamp": 1, "median_minutes": None},
        {"category": "transport", "samples": 2, "missing_timestamp": 1, "median_minutes": 27.0},
    ]
    assert [row["stages"] for row in rows] == [
        {
            "collection": {"samples": 0, "median_minutes": None},
            "before_post_record": {"samples": 0, "median_minutes": None},
            "after_post_record": {"samples": 0, "median_minutes": None},
            "since_ingestion": {"samples": 1, "median_minutes": 90.0},
        },
        {
            "collection": {"samples": 2, "median_minutes": 17.0},
            "before_post_record": {"samples": 3, "median_minutes": 6.0},
            "after_post_record": {"samples": 3, "median_minutes": 4.0},
            "since_ingestion": {"samples": 3, "median_minutes": 10.0},
        },
    ]
    assert "SECRET" not in json.dumps(daily)
    assert "PRIVATE_SOURCE" not in json.dumps(daily)
    assert "27.0 | 2 | 1" in markdown(daily)
    assert "2026-11-22 (неполный)" in markdown(daily)
    assert "17.0 (2) | 6.0 (3) | 4.0 (3) | 10.0 (3)" in markdown(daily)
    assert "| 2026-11-22 (неполный) | 4 | — | 4 (100 %) |" in markdown(daily)
    assert "| events | — | — | — | 90.0 (1) |" in markdown(daily)


def test_publication_cadence_counts_receipts_and_includes_both_daytime_edges():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    today = datetime(2026, 11, 22, 12, tzinfo=ZONE)
    previous = today - timedelta(days=1)
    with Session(engine) as session:
        session.add_all([source(OFFICIAL), source("reader", "reader")])
        session.flush()
        for number, at, status, source_id in (
            (1, previous.replace(hour=9), "sent", OFFICIAL),
            (2, previous.replace(hour=12), "sent", OFFICIAL),
            (2, previous.replace(hour=18), "sent", OFFICIAL),
            (3, previous.replace(hour=23), "sent", OFFICIAL),
            (4, today.replace(hour=7), "sent", OFFICIAL),
            (5, today.replace(hour=8, minute=30), "sent", "reader"),
            (6, today.replace(hour=10), "sent", OFFICIAL),
            (7, today.replace(hour=11), "sending", OFFICIAL),
            (8, today.replace(hour=13), "sent", OFFICIAL),
        ):
            raw = item(session, source_id, at, at)
            sent(session, raw, at, number, status=status)
        session.commit()
        report = editorial_flow(session, "belgrade", {OFFICIAL}, now=today, zone=ZONE)

    empty, complete, current = report["days"][-3:]
    assert empty["publication_cadence"] == {
        "posts": 0, "quiet_hour_posts": 0, "max_daytime_gap_minutes": 780.0,
    }
    assert complete["publication_cadence"] == {
        "posts": 3, "quiet_hour_posts": 1, "max_daytime_gap_minutes": 540.0,
    }
    assert current["publication_cadence"] == {
        "posts": 3, "quiet_hour_posts": 1, "max_daytime_gap_minutes": 120.0,
    }


def test_night_hold_observed_on_belgrade_delivery_day():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    published = MIDNIGHT + timedelta(hours=8, minutes=10)
    with Session(engine) as session:
        session.add(source(OFFICIAL))
        session.flush()
        ingested = MIDNIGHT - timedelta(minutes=50)
        raw = item(session, OFFICIAL, ingested - timedelta(minutes=5), ingested)
        sent(session, raw, published, 6, category="events",
             created_at=MIDNIGHT - timedelta(minutes=40))
        session.commit()
        report = editorial_flow(session, "belgrade", {OFFICIAL}, now=published, zone=ZONE)
    assert report["days"][-2]["delivery_latency"] == []
    assert report["days"][-1]["local_date"] == "2026-11-22"
    assert report["days"][-1]["delivery_latency"][0]["stages"] == {
        "collection": {"samples": 1, "median_minutes": 5.0},
        "before_post_record": {"samples": 1, "median_minutes": 10.0},
        "after_post_record": {"samples": 1, "median_minutes": 530.0},
        "since_ingestion": {"samples": 1, "median_minutes": 540.0},
    }


def test_previous_snapshots_keep_the_existing_latency_shape():
    old = {
        "observed_at": NOW.isoformat(), "timezone": ZONE.key, "days": [{
            "local_date": "2026-11-22", "expired_unreviewed": [],
            "delivery_latency": [{
                "category": "transport", "samples": 0,
                "missing_timestamp": 1, "median_minutes": None,
            }],
        }],
    }
    assert public_editorial_flow(old, {OFFICIAL}) == {
        **old, "observed_at": "2026-11-22T00:30:00Z",
    }
