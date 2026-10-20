from datetime import UTC, datetime

import pytest
import test_ingestion_persistence as ingestion_cases
import test_package_import as package_cases
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_editorial_integration import prepare_service
from test_editorial_package import bind_context, package_payload, parse

from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.ingestion.persistence import capture_collected_batch
from relocant_media.services.editorial import DeliveryBlockedError
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import RawItem, SourceRevision
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def sqlite_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'editorial.sqlite3'}")
    initialize_sqlite(tmp_path / "editorial.sqlite3")
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize("secondary", [False, True])
def test_sqlite_preserves_receipts_and_queues_secondary_source_changes(sqlite_session, secondary):
    case = ingestion_cases.test_reviewed_changes_preserve_evidence_and_receipts
    case(sqlite_session, secondary)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("high_risk", [False, True])
def test_sqlite_event_bundle_keeps_v1_hold_and_v2_source_policy(
    sqlite_session, version, high_risk,
):
    case = package_cases.test_package_v1_does_not_approve_unreviewed_event_bundle
    case(sqlite_session, high_risk, version)


@pytest.mark.asyncio
async def test_sqlite_package_delivery_is_blocked_after_source_revision(sqlite_session):
    session = sqlite_session
    service, provider, item_id = prepare_service(session, category="transport")
    payload = package_payload()
    payload["raw_item_id"] = item_id
    for review in payload["reviews"]:
        review["reviewed_at"] = datetime.now(UTC).isoformat()
    bind_context(payload)
    run = import_package(session, parse(payload))
    assert (await service.deliver(item_id, dry_run=True)).status == "dry_run"
    session.commit()
    item = session.get(RawItem, item_id)
    capture_collected_batch(session, [CollectedItem(item.source_id, item.external_id,
                           item.url, item.title, item.body + " changed", item.published_at)])
    session.commit()
    with pytest.raises(DeliveryBlockedError):
        await service.deliver(item_id, dry_run=True)
    assert session.scalar(select(SourceRevision)).evidence["body"].endswith(" changed")
    assert run.delivery_status == "pending"
    assert provider.calls == []


def test_sqlite_roundtrip_date_and_json_are_exact(sqlite_session):
    session = sqlite_session
    _, _, item_id = prepare_service(session)
    item = session.get(RawItem, item_id)
    item.published_at = datetime(2026, 10, 20, 8, tzinfo=UTC)
    session.commit()
    session.expire_all()
    assert session.get(RawItem, item_id).published_at == datetime(2026, 10, 20, 8, tzinfo=UTC)


def test_sqlite_exports_candidates_and_revisions_without_changing_decisions(
    sqlite_session, tmp_path, monkeypatch,
):
    from datetime import timedelta

    from relocant_media.ops.candidates_cli import export_candidates
    from relocant_media.ops.revisions_cli import export_revisions

    session = sqlite_session
    service, _, item_id = prepare_service(session, category="transport")
    item = session.get(RawItem, item_id)
    now = datetime.now(UTC)
    item.published_at = now
    session.commit()
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", str(session.get_bind().url))
    result = export_candidates(city="belgrade", since=now - timedelta(days=1),
                               until=now + timedelta(minutes=1), limit=5,
                               output=tmp_path / "candidates")
    assert result["candidate_count"] == 1 and result["version"] == 2
    run = service.prepare(item_id)
    capture_collected_batch(session, [CollectedItem(item.source_id, item.external_id,
                           item.url, item.title, item.body + " changed", item.published_at)])
    session.commit()
    exported = export_revisions(city="belgrade", output=tmp_path / "revisions")
    assert exported["revision_count"] == 1
    assert exported["revisions"][0]["approval"] is False
    session.refresh(run)
    assert run.outcome["reason"] == "source_changed_requires_review"
    assert run.delivery_status == "pending"
