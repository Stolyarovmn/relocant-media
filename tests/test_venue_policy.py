from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_editorial_integration import prepare_service
from test_editorial_package import bind_context, package_payload, parse

from relocant_media.domain.dedup import event_key
from relocant_media.services.editorial import (
    DeliveryBlockedError,
    _auto_publish_policy_reason,
    _candidate,
    _snapshot_member,
)
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import RawItem, Source
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def venue(tmp_path):
    path = tmp_path / "venue.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        service, _, item_id = prepare_service(session)
        item = session.get(RawItem, item_id)
        source = session.get(Source, item.source_id)
        source.source_type = "official_venue_events"
        source.ownership = "official"
        source.auto_publish = True
        source.high_risk = False
        session.commit()
        yield session, service, item, source
    engine.dispose()


def venue_package(item, source, category="events"):
    payload = package_payload()
    payload.update(version=2, policy_version="subscription-editorial-v2", raw_item_id=item.id)
    payload["outcome"]["ranking"].update(category=category, confidence=1)
    payload["outcome"]["confidence"] = 1
    payload["event"] = {
        "event_key": event_key(_candidate(item)),
        "editorial_rule_version": "belgrade-editorial-v2",
        "documents": [_snapshot_member(item, source)],
    }
    for review in payload["reviews"]:
        review["reviewed_at"] = datetime.now(UTC).isoformat()
        review["score"] = 100
    bind_context(payload)
    return parse(payload)


def test_two_reviewed_venue_event_is_ready(venue):
    session, _, item, source = venue
    package = venue_package(item, source)
    assert {r.role for r in package.reviews} == {"facts", "utility"}
    assert import_package(session, package).outcome["status"] == "ready"


def test_perfect_reviews_cannot_allow_venue_transport(venue):
    session, _, item, source = venue
    run = import_package(session, venue_package(item, source, "transport"))
    assert run.outcome["status"] == "human_review"
    assert run.outcome["reason"] == "venue_events_only"


def test_high_risk_venue_event_still_held(venue):
    session, _, item, source = venue
    source.high_risk = True
    session.commit()
    run = import_package(session, venue_package(item, source))
    assert run.outcome["status"] == "human_review"
    assert run.outcome["reason"] == "source_policy_requires_review"


def test_prepare_enforces_venue_scope(venue):
    _, service, item, _ = venue
    run = service.prepare(item.id)  # Fixture ranks the article as utilities.
    assert run.outcome["status"] == "human_review"
    assert run.outcome["reason"] == "venue_events_only"


@pytest.mark.asyncio
async def test_delivery_rechecks_current_venue_scope(venue):
    session, service, item, source = venue
    source.source_type = "official"
    session.commit()
    run = import_package(session, venue_package(item, source, "transport"))
    assert run.outcome["status"] == "ready"
    source.source_type = "official_venue_events"
    session.commit()
    with pytest.raises(DeliveryBlockedError, match="venue_events_only"):
        await service.deliver(item.id)


def test_unknown_category_fails_closed_and_government_is_unchanged(venue):
    _, _, item, source = venue
    reason = _auto_publish_policy_reason([(item, source)], anchor_source=source)
    assert reason == "venue_events_only"
    source.source_type = "official"
    assert _auto_publish_policy_reason([(item, source)], anchor_source=source) is None


def test_review_rejection_cannot_be_overridden_by_venue_policy(venue):
    session, _, item, source = venue
    payload = venue_package(item, source).model_dump(mode="json")
    payload["reviews"][0]["verdict"] = "hold"
    run = import_package(session, parse(payload))
    assert run.outcome["status"] != "ready"
