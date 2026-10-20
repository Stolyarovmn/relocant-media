from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service
from test_editorial_package import NOW, bind_context, package_payload, parse

from relocant_media.services.package_import import PackageConflictError, import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

editorial_session = _editorial_session


@pytest.mark.parametrize('peer_kind', ['import', 'prepare'])
def test_parallel_entry_points_create_only_one_result_for_an_event(
    editorial_session: Session, monkeypatch: pytest.MonkeyPatch, peer_kind: str,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep

    from sqlalchemy import func, select

    from relocant_media.services import editorial, package_import

    service, _, item_id = prepare_service(editorial_session)
    item = editorial_session.get(RawItem, item_id)
    assert item is not None
    duplicate = RawItem(source_id=item.source_id, external_id='concurrent-copy',
                        url=item.url + '?copy=1', title=item.title, body=item.body)
    editorial_session.add(duplicate)
    editorial_session.commit()
    payload = package_payload()
    payload['raw_item_id'] = duplicate.id
    payload['article_id'] = duplicate.external_id
    payload['evidence']['url'] = duplicate.url
    for claim in payload['outcome']['claims']['claims']:
        for citation in claim['citations']:
            citation['url'] = duplicate.url
    for review in payload['reviews']:
        review['article_id'] = duplicate.external_id
    bind_context(payload)
    copy_package = parse(payload)
    original_payload = package_payload()
    original_payload['raw_item_id'] = item_id
    bind_context(original_payload)
    original_package = parse(original_payload)
    barrier = Barrier(2, timeout=5)
    lock = editorial._lock_editorial_city
    check = editorial._reviewed_event_item

    def concurrent_lock(session, city):
        barrier.wait()
        lock(session, city)

    def slow_check(*args, **kwargs):
        result = check(*args, **kwargs)
        # Keep the check/create gap open while the peer attempts the same lock.
        sleep(0.2)
        return result

    monkeypatch.setattr(editorial, '_lock_editorial_city', concurrent_lock)
    monkeypatch.setattr(package_import, '_lock_editorial_city', concurrent_lock)
    monkeypatch.setattr(editorial, '_reviewed_event_item', slow_check)
    monkeypatch.setattr(package_import, '_reviewed_event_item', slow_check)
    engine = editorial_session.get_bind()

    def worker(is_copy):
        with Session(engine) as session:
            try:
                if not is_copy and peer_kind == 'prepare':
                    editorial.EditorialService(session, service.pipeline).prepare(item_id)
                else:
                    import_package(session, copy_package if is_copy else original_package, now=NOW)
                return 'created'
            except ValueError as error:
                assert 'event already reviewed' in str(error)
                return 'duplicate'

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, value) for value in (False, True)]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(results) == ['created', 'duplicate']
    assert editorial_session.scalar(select(func.count()).select_from(EditorialRun)) == 1
    assert editorial_session.scalar(select(func.count()).select_from(Post)) == 1


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
@pytest.mark.parametrize('version', [1, 2])
def test_package_v1_does_not_approve_unreviewed_event_bundle(
    editorial_session: Session, high_risk: bool, version: int,
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
    if version == 2:
        from relocant_media.domain.dedup import event_key
        from relocant_media.services.editorial import _candidate, _snapshot_member

        payload.update(version=2, policy_version='subscription-editorial-v2')
        payload['event'] = {
            'event_key': event_key(_candidate(original)),
            'editorial_rule_version': 'belgrade-editorial-v2',
            'documents': [_snapshot_member(
                              original, editorial_session.get(Source, original.source_id)),
                          _snapshot_member(other, source)],
        }
    bind_context(payload)
    package = parse(payload)
    run = import_package(editorial_session, package, now=NOW)
    expected_status = 'ready' if version == 2 and not high_risk else 'human_review'
    assert run.outcome['status'] == expected_status
    assert run.outcome['reason'] == (
        'source_policy_requires_review' if high_risk else
        'package_requires_event_bundle_review' if version == 1 else 'package_independently_reviewed'
    )
    assert {d['raw_item_id'] for d in run.evidence['documents']} == {item_id, other.id}
    assert run.evidence['event_key']
    assert run.editorial_rule_version == 'belgrade-editorial-v2'
    assert editorial_session.get(Post, run.post_id).status == expected_status
    assert import_package(editorial_session, package, now=NOW).post_id == run.post_id


@pytest.mark.parametrize('mutation', ['omitted', 'changed', 'policy', 'key', 'unrelated'])
def test_v2_import_rechecks_complete_event_against_database(
    editorial_session: Session, mutation: str,
) -> None:
    from relocant_media.domain.dedup import event_key
    from relocant_media.services.editorial import _candidate, _snapshot_member

    _, _, item_id = prepare_service(editorial_session)
    item = editorial_session.get(RawItem, item_id)
    item.published_at = NOW
    source = Source(id='secondary', city='belgrade', name='Secondary',
                    url='https://secondary.example', source_type='official', trust=1,
                    ownership='official', high_risk=False, auto_publish=True, enabled=True)
    editorial_session.add(source)
    editorial_session.flush()
    other = RawItem(source_id=source.id, external_id='event', url=source.url + '/event',
                    title=item.title, body=item.body, published_at=NOW)
    editorial_session.add(other)
    editorial_session.commit()
    payload = package_payload()
    payload.update(version=2, policy_version='subscription-editorial-v2', raw_item_id=item_id)
    payload['evidence']['published_at'] = NOW.isoformat()
    payload['event'] = {
        'event_key': event_key(_candidate(item)),
        'editorial_rule_version': 'belgrade-editorial-v2',
        'documents': [_snapshot_member(item, editorial_session.get(Source, item.source_id)),
                      _snapshot_member(other, source)],
    }
    if mutation == 'omitted':
        payload['event']['documents'].pop()
    elif mutation == 'changed':
        payload['event']['documents'][1]['body'] += ' changed'
    elif mutation == 'policy':
        source.high_risk = True
        editorial_session.commit()
    elif mutation == 'unrelated':
        other.title = 'Unrelated subject'
        other.body = 'Different event entirely'
        editorial_session.commit()
    else:
        payload['event']['event_key'] = 'b' * 64
    bind_context(payload)
    with pytest.raises(PackageConflictError, match='event evidence or source policy changed'):
        import_package(editorial_session, parse(payload), now=NOW)
    assert editorial_session.get(EditorialRun, item_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['new_source', 'policy', 'disabled', 'body'])
async def test_v2_delivery_rechecks_event_membership_and_policy(
    editorial_session: Session, mutation: str,
) -> None:
    from relocant_media.domain.dedup import event_key
    from relocant_media.services.editorial import (
        DeliveryBlockedError,
        _candidate,
        _snapshot_member,
    )

    service, _, item_id = prepare_service(editorial_session)
    item = editorial_session.get(RawItem, item_id)
    item.published_at = NOW
    source = editorial_session.get(Source, item.source_id)
    payload = package_payload()
    payload.update(version=2, policy_version='subscription-editorial-v2', raw_item_id=item_id)
    payload['evidence']['published_at'] = NOW.isoformat()
    payload['event'] = {
        'event_key': event_key(_candidate(item)),
        'editorial_rule_version': 'belgrade-editorial-v2',
        'documents': [_snapshot_member(item, source)],
    }
    for review in payload['reviews']:
        review['reviewed_at'] = datetime.now(UTC).isoformat()
    bind_context(payload)
    run = import_package(editorial_session, parse(payload))
    assert (await service.deliver(item_id, dry_run=True)).status == 'dry_run'
    if mutation == 'new_source':
        other_source = Source(id='later', city='belgrade', name='Later',
                              url='https://later.example', source_type='official', trust=1,
                              ownership='official', high_risk=True, auto_publish=True, enabled=True)
        editorial_session.add(other_source)
        editorial_session.flush()
        editorial_session.add(RawItem(source_id=other_source.id, external_id='event',
                                     url=other_source.url + '/event', title=item.title,
                                     body=item.body, published_at=NOW))
    elif mutation == 'policy':
        source.ownership = 'unknown'
    elif mutation == 'disabled':
        source.enabled = False
    else:
        item.body += ' Changed'
    editorial_session.commit()
    with pytest.raises(DeliveryBlockedError):
        await service.deliver(item_id, dry_run=True)
    assert run.delivery_status == 'pending' and run.staging_message_id is None


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
