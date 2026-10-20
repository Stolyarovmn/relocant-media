import asyncio
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime
from threading import Event

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service

from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.ingestion.persistence import capture_collected_batch
from relocant_media.services.editorial import DeliveryBlockedError, EditorialService
from relocant_media.storage.models import EditorialRun, RawItem, Source, SourceRevision

editorial_session = _editorial_session


def test_capture_committed_before_delivery_claim_blocks_stale_cached_run(
    editorial_session: Session,
) -> None:
    service, _, item_id = prepare_service(editorial_session, category="transport")
    run = service.prepare(item_id)
    item = editorial_session.get(RawItem, item_id)
    changed = CollectedItem(item.source_id, item.external_id, item.url, item.title,
                            item.body + " changed", item.published_at)
    engine = editorial_session.get_bind()
    captured, commit_allowed, delivering = Event(), Event(), Event()

    def capture():
        with Session(engine) as session:
            capture_collected_batch(session, [changed])
            captured.set()
            assert commit_allowed.wait(5)
            session.commit()

    def deliver():
        with Session(engine) as session:
            # Populate stale cached state before waiting for the collector's lock.
            stale = session.get(EditorialRun, item_id)
            assert stale.outcome["status"] == "ready"
            delivering.set()
            with pytest.raises(DeliveryBlockedError):
                asyncio.run(EditorialService(session, service.pipeline).deliver(
                    item_id, dry_run=True,
                ))

    with ThreadPoolExecutor(max_workers=2) as pool:
        collector = pool.submit(capture)
        assert captured.wait(5)
        sender = pool.submit(deliver)
        assert delivering.wait(5)
        with pytest.raises(TimeoutError):
            sender.result(timeout=0.1)
        commit_allowed.set()
        collector.result(timeout=5)
        sender.result(timeout=5)
    editorial_session.refresh(run)
    assert run.delivery_status == "pending" and run.staging_message_id is None
    assert editorial_session.scalar(select(func.count()).select_from(SourceRevision)) == 1


def test_prepare_and_secondary_source_capture_serialize_without_losing_revision(
    editorial_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _, item_id = prepare_service(editorial_session, category="transport")
    item = editorial_session.get(RawItem, item_id)
    item.published_at = datetime.now(UTC)
    source = Source(id="secondary", city="belgrade", name="Secondary",
                    url="https://secondary.example", source_type="official", trust=1,
                    ownership="official", high_risk=False, auto_publish=True, enabled=True)
    editorial_session.add(source)
    editorial_session.flush()
    secondary = RawItem(source_id=source.id, external_id="same-event", url=source.url + "/event",
                        title=item.title, body=item.body, published_at=item.published_at)
    editorial_session.add(secondary)
    editorial_session.commit()
    secondary_id = secondary.id
    change = CollectedItem(secondary.source_id, secondary.external_id, secondary.url,
                           secondary.title, secondary.body + " changed", secondary.published_at)
    entered, proceed, collector_started = Event(), Event(), Event()
    original_run = service.pipeline.run

    def paused_pipeline(**kwargs):
        entered.set()
        assert proceed.wait(5)
        return original_run(**kwargs)

    monkeypatch.setattr(service.pipeline, "run", paused_pipeline)
    engine = editorial_session.get_bind()

    def prepare():
        with Session(engine) as session:
            return EditorialService(session, service.pipeline).prepare(item_id).raw_item_id

    def capture():
        with Session(engine) as session:
            collector_started.set()
            result = capture_collected_batch(session, [change])
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        preparing = pool.submit(prepare)
        assert entered.wait(5)
        collecting = pool.submit(capture)
        assert collector_started.wait(5)
        with pytest.raises(TimeoutError):
            collecting.result(timeout=0.1)
        proceed.set()
        assert preparing.result(timeout=5) == item_id
        assert collecting.result(timeout=5) == (0, 1)
    editorial_session.expire_all()
    revision = editorial_session.scalar(select(SourceRevision))
    assert revision is not None and revision.raw_item_id == secondary_id
    assert editorial_session.get(RawItem, secondary_id).body == item.body
    run = editorial_session.get(EditorialRun, item_id)
    assert run.outcome["reason"] == "source_changed_requires_review"
    assert run.delivery_status == "pending"


def test_revision_export_is_read_only_and_does_not_reuse_approval(
    editorial_session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from relocant_media.ops.revisions_cli import export_revisions

    service, _, item_id = prepare_service(editorial_session)
    run = service.prepare(item_id)
    item = editorial_session.get(RawItem, item_id)
    capture_collected_batch(editorial_session, [CollectedItem(
        item.source_id, item.external_id, item.url, item.title, item.body + " new", None,
    )])
    editorial_session.commit()
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    output = tmp_path / "revisions"
    result = export_revisions(city="belgrade", output=output)
    assert result["revision_count"] == 1
    revision = result["revisions"][0]
    assert revision["approval"] is False
    assert revision["status"] == "requires_fresh_review"
    assert revision["previous_evidence"]["body"] == item.body
    assert revision["evidence"]["body"] == item.body + " new"
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == result
    assert run.delivery_status == "pending"
    with pytest.raises(ValueError):
        export_revisions(city="belgrade", output=output)
    assert export_revisions(city="other", output=tmp_path / "other")["revision_count"] == 0
