from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service
from test_editorial_package import NOW, bind_context, package_payload, parse

from relocant_media.services.package_import import PackageConflictError, import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

editorial_session = _editorial_session


@pytest.mark.parametrize('legacy', [False, True])
def test_package_import_rejects_an_event_already_reviewed_under_another_url(
    editorial_session: Session, legacy: bool,
) -> None:
    _, _, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload['raw_item_id'] = item_id
    bind_context(payload)
    first = import_package(editorial_session, parse(payload), now=NOW)
    first.delivery_status = 'sent'
    first.staging_message_id = 42
    original = editorial_session.get(RawItem, item_id)
    assert original is not None
    if legacy:
        original.canonical_url = None
        original.content_sha256 = None
    duplicate = RawItem(source_id=original.source_id, external_id='another-url',
                        url=original.url + '?copy=1', title=original.title,
                        body=original.body, published_at=original.published_at)
    editorial_session.add(duplicate)
    editorial_session.commit()
    payload['raw_item_id'] = duplicate.id
    payload['article_id'] = duplicate.external_id
    payload['evidence']['url'] = duplicate.url
    for claim in payload['outcome']['claims']['claims']:
        for citation in claim['citations']:
            citation['url'] = duplicate.url
    for review in payload['reviews']:
        review['article_id'] = duplicate.external_id
    bind_context(payload)
    with pytest.raises(PackageConflictError, match='event already reviewed'):
        import_package(editorial_session, parse(payload), now=NOW)
    assert editorial_session.get(EditorialRun, duplicate.id) is None
    editorial_session.refresh(first)
    assert first.delivery_status == 'sent' and first.staging_message_id == 42


@pytest.mark.parametrize('high_risk', [False, True])
def test_package_v1_does_not_approve_unreviewed_event_bundle(
    editorial_session: Session, high_risk: bool,
) -> None:
    _, _, item_id = prepare_service(editorial_session)
    original = editorial_session.get(RawItem, item_id)
    assert original is not None
    original.published_at = NOW
    source = Source(id='other-event-source', city='belgrade', name='Other source',
                    url='https://other.example', source_type='official', trust=1,
                    ownership='official', high_risk=high_risk, auto_publish=True, enabled=True)
    editorial_session.add(source)
    editorial_session.flush()
    other = RawItem(source_id=source.id, external_id='same-event',
                    url='https://other.example/event', title=original.title,
                    body=original.body, published_at=NOW)
    editorial_session.add(other)
    editorial_session.commit()
    payload = package_payload()
    payload['raw_item_id'] = item_id
    payload['evidence']['published_at'] = NOW.isoformat()
    bind_context(payload)
    package = parse(payload)
    run = import_package(editorial_session, package, now=NOW)
    assert run.outcome['status'] == 'human_review'
    assert run.outcome['reason'] == (
        'source_policy_requires_review' if high_risk else 'package_requires_event_bundle_review'
    )
    assert {d['raw_item_id'] for d in run.evidence['documents']} == {item_id, other.id}
    assert run.evidence['event_key']
    assert run.editorial_rule_version == 'belgrade-editorial-v2'
    assert editorial_session.get(Post, run.post_id).status == 'human_review'
    assert import_package(editorial_session, package, now=NOW).post_id == run.post_id


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["dry-run", "fake"])
async def test_package_cli_preview_delivery_without_ai_or_real_network(
    editorial_session: Session, monkeypatch: pytest.MonkeyPatch, transport: str,
) -> None:
    import builtins

    import httpx

    from relocant_media.ops.package_cli import _persist
    from relocant_media.storage import db
    from relocant_media.storage.models import EditorialRun

    _, provider, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = item_id
    for review in payload["reviews"]:
        review["reviewed_at"] = datetime.now(UTC).isoformat()
    bind_context(payload)
    package = parse(payload)
    monkeypatch.setattr(db, "SessionFactory", lambda: editorial_session)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "relocant_media.ai.gemini":
            raise AssertionError("Gemini must not be initialized")
        return original_import(name, *args, **kwargs)

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("real HTTP transport must not be called")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network)
    first = await _persist(package, transport)
    second = await _persist(package, transport)
    assert first == second
    assert first["delivery"]["status"] == "dry_run"
    if transport == "fake":
        requests = first["fake_requests"]
        assert len(requests) == 1
        assert requests[0]["chat_id"] == "-100000000001"
        assert requests[0]["text"] == first["delivery"]["text"]
    else:
        assert "fake_requests" not in first
    run = editorial_session.get(EditorialRun, item_id)
    assert run is not None
    assert run.delivery_status == "pending" and run.staging_message_id is None
    assert provider.calls == []


def test_package_import_is_idempotent_and_rejects_new_draft(editorial_session: Session) -> None:
    _, provider, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = item_id
    bind_context(payload)
    package = parse(payload)
    run = import_package(editorial_session, package, now=NOW)
    assert run.outcome["status"] == "ready" and run.post_id is not None
    assert run.delivery_status == "pending" and run.staging_message_id is None
    assert import_package(editorial_session, package, now=NOW).post_id == run.post_id
    assert provider.calls == []
    payload["reviews"][0]["score"] = 81
    with pytest.raises(PackageConflictError):
        import_package(editorial_session, parse(payload), now=NOW)


def test_changed_raw_evidence_rejects_package(editorial_session: Session) -> None:
    _, _, item_id = prepare_service(editorial_session)
    item = editorial_session.get(RawItem, item_id)
    assert item is not None
    item.body += " updated"
    editorial_session.commit()
    payload = package_payload()
    payload["raw_item_id"] = item_id
    bind_context(payload)
    with pytest.raises(PackageConflictError, match="evidence changed"):
        import_package(editorial_session, parse(payload), now=NOW)


@pytest.mark.asyncio
async def test_persisted_package_reviews_expire_before_staging_delivery(
    editorial_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relocant_media.ai import package as package_module
    from relocant_media.services.editorial import DeliveryBlockedError

    service, _, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = item_id
    bind_context(payload)
    import_package(editorial_session, parse(payload), now=NOW)
    evaluate = package_module.evaluate_package
    monkeypatch.setattr(package_module, "evaluate_package",
                        lambda package: evaluate(package, now=NOW + timedelta(hours=25)))
    with pytest.raises(DeliveryBlockedError, match="human_review"):
        await service.deliver(item_id)


@pytest.mark.asyncio
async def test_collector_change_blocks_package_even_after_identical_reimport(
    editorial_session: Session,
) -> None:
    from pathlib import Path

    from relocant_media.collectors.base import RawItem as CollectedItem
    from relocant_media.ingestion.persistence import (
        ReviewedEvidenceChangedError,
        record_collector_failure,
        upsert_raw_items,
    )
    from relocant_media.services.editorial import DeliveryBlockedError
    from relocant_media.sources.registry import load_source_file

    service, provider, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = item_id
    for review in payload["reviews"]:
        review["reviewed_at"] = datetime.now(UTC).isoformat()
    bind_context(payload)
    package = parse(payload)
    original = import_package(editorial_session, package)
    item = editorial_session.get(RawItem, item_id)
    incoming = CollectedItem(item.source_id, item.external_id, item.url, item.title,
                             item.body + " changed", item.published_at)
    with pytest.raises(ReviewedEvidenceChangedError) as caught:
        upsert_raw_items(editorial_session, [incoming])
    editorial_session.rollback()
    source = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
    record_collector_failure(editorial_session, source, caught.value)
    editorial_session.commit()
    repeated = import_package(editorial_session, package)
    assert repeated.post_id == original.post_id
    with pytest.raises(DeliveryBlockedError, match="post is no longer ready"):
        await service.deliver(item_id, dry_run=True)
    assert repeated.delivery_status == "pending" and repeated.staging_message_id is None
    assert provider.calls == []
