from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ops.newsroom_export import snapshot
from relocant_media.ops.newsroom_merge import merge
from relocant_media.services.kpi import compute_kpi, record_subscriber_observation
from relocant_media.storage.models import Base, ChannelSnapshot, Post, Source

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _post(session: Session, *, hours_ago: float | None, message_id: int | None) -> None:
    session.add(Post(
        city="belgrade", category="events", status="ready", title="t", body="b",
        confidence=Decimal("0.9"), editorial_rule_version="v",
        telegram_message_id=message_id,
        published_at=None if hours_ago is None else NOW - timedelta(hours=hours_ago),
    ))


def _subscribers(session: Session, hours_ago: float, count: int) -> None:
    session.add(ChannelSnapshot(
        city="belgrade", captured_at=NOW - timedelta(hours=hours_ago),
        subscribers=count, source="telegram_bot_api", kind="observed",
    ))


def test_unknown_audience_stays_unknown_instead_of_zero() -> None:
    with _session() as session:
        kpi = compute_kpi(session, "belgrade", now=NOW)
    assert kpi["subscribers"] is None
    assert kpi["published"] == {
        "last_24h": 0, "last_7d": 0, "last_published_at": None, "hours_since_last": None,
    }


def test_kpi_counts_only_confirmed_deliveries_and_growth() -> None:
    with _session() as session:
        _post(session, hours_ago=2, message_id=11)
        _post(session, hours_ago=30, message_id=12)
        _post(session, hours_ago=200, message_id=13)
        _post(session, hours_ago=1, message_id=None)   # no receipt: not a publication
        _post(session, hours_ago=None, message_id=14)  # no publication time: unknown
        _subscribers(session, 200, 2)
        _subscribers(session, 30, 10)
        _subscribers(session, 0.5, 25)
        session.commit()
        kpi = compute_kpi(session, "belgrade", now=NOW)
    assert kpi["published"]["last_24h"] == 1
    assert kpi["published"]["last_7d"] == 2
    assert kpi["published"]["hours_since_last"] == 2.0
    assert kpi["subscribers"] == {
        "count": 25, "observed_at": (NOW - timedelta(hours=0.5)).isoformat(),
        "delta_24h": 15, "delta_7d": 23,
    }


def test_growth_is_unknown_without_an_older_observation() -> None:
    with _session() as session:
        _subscribers(session, 1, 7)
        session.commit()
        kpi = compute_kpi(session, "belgrade", now=NOW)
    assert kpi["subscribers"]["count"] == 7
    assert kpi["subscribers"]["delta_24h"] is None and kpi["subscribers"]["delta_7d"] is None


def test_only_observed_member_counts_are_persisted() -> None:
    with _session() as session:
        record_subscriber_observation(session, "belgrade", {
            "status": "observed", "subscribers": 42, "source": "telegram_bot_api",
            "observed_at": NOW.isoformat(),
        })
        record_subscriber_observation(session, "belgrade", {"status": "unavailable"})
        record_subscriber_observation(session, "belgrade", {
            "status": "observed", "subscribers": -1, "observed_at": NOW.isoformat(),
        })
        rows = session.scalars(select(ChannelSnapshot)).all()
    assert [(row.subscribers, row.kind) for row in rows] == [(42, "observed")]


def test_public_projection_carries_kpi_through_merge(monkeypatch) -> None:
    root = Path(".")
    real_now = datetime.now(UTC)
    monkeypatch.setattr(f"{__name__}.NOW", real_now)
    with _session() as session:
        session.add(Source(id="official", city="belgrade", name="o", url="https://example.org/",
                           source_type="news", trust=Decimal("1"), ownership="official"))
        _subscribers(session, 0.5, 25)
        _post(session, hours_ago=2, message_id=11)
        session.commit()
        runtime = snapshot(root, session)
    assert runtime["cities"][0]["kpi"]["subscribers"]["count"] == 25
    runtime["cities"][0]["kpi"]["secret"] = "TOKEN"
    merged = merge(snapshot(root, None), runtime)
    kpi = merged["cities"][0]["kpi"]
    assert kpi["subscribers"]["count"] == 25 and kpi["published"]["last_7d"] == 1
    assert "TOKEN" not in str(merged)
