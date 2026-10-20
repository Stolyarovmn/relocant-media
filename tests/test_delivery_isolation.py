import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_channel_delivery import reviewed as reviewed_fixture
from test_editorial_package import bind_context, parse

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import draft_sha256
from relocant_media.domain.dedup import event_key
from relocant_media.domain.freshness import FreshnessDecision
from relocant_media.ops import server_cycle
from relocant_media.services.editorial import _candidate, _document, _snapshot_member
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

reviewed = reviewed_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["live_changed", "expired_review"])
async def test_blocked_first_package_does_not_block_second_valid_delivery(
    reviewed, monkeypatch, first_failure,
):
    session, first = reviewed
    original = session.get(RawItem, first.raw_item_id)
    source = session.get(Source, original.source_id)
    second_item = RawItem(source_id=source.id, external_id="another-valid-event",
        url=original.url + "-another-event", title="Отдельное транспортное сообщение",
        body=original.body + " Дополнительные сведения об отдельном событии.",
        published_at=original.published_at)
    session.add(second_item)
    session.flush()
    payload = copy.deepcopy(first.outcome["_package"])
    payload.update(raw_item_id=second_item.id, article_id=second_item.external_id)
    payload["evidence"] = _document(second_item).model_dump(mode="json")
    payload["event"].update(event_key=event_key(_candidate(second_item)),
        documents=[_snapshot_member(second_item, source)])
    for claim in payload["outcome"]["claims"]["claims"]:
        for citation in claim["citations"]:
            citation["url"] = second_item.url
    payload["outcome"]["draft"]["title"] = "[тестовый запуск] Второе сообщение"
    digest = draft_sha256(parse(payload).outcome.draft)
    for review in payload["reviews"]:
        review.update(article_id=second_item.external_id, draft_sha256=digest,
                      reviewed_at=datetime.now(UTC).isoformat())
    bind_context(payload)
    second = import_package(session, parse(payload))
    if first_failure == "expired_review":
        first_payload = copy.deepcopy(first.outcome)
        for review in first_payload["_package"]["reviews"]:
            review["reviewed_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        first.outcome = first_payload
        session.commit()
    original_outcome = copy.deepcopy(first.outcome)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(server_cycle, "freshness_decision",
        lambda **kwargs: FreshnessDecision("keep", "test_current"))

    def live(package):
        docs = tuple(EvidenceDocument.model_validate(doc.model_dump())
                     for doc in package.event.documents)
        if package.raw_item_id == first.raw_item_id and first_failure == "live_changed":
            return (docs[0].model_copy(update={"body": "Изменившийся источник"}),)
        return docs

    monkeypatch.setattr(server_cycle, "fetch_documents", live)
    calls = []

    class Bot:
        def __init__(self, *args):
            pass

        async def send_message(self, target, text):
            with Session(session.get_bind()) as check:
                assert check.get(EditorialRun, second.raw_item_id).delivery_status == "sending"
            calls.append((target, text))
            return 101

    monkeypatch.setattr(server_cycle, "TelegramBotAPI", Bot)
    blocked = []
    assert await server_cycle.publish_pending(
        session.get_bind(), enabled=True, blocked=blocked) == [101]
    assert blocked[0]["raw_item_id"] == first.raw_item_id
    assert blocked[0]["reason"] == (
        "live_source_check_failed" if first_failure == "live_changed" else "delivery_guard_blocked")
    session.expire_all()
    first = session.get(EditorialRun, first.raw_item_id)
    second = session.get(EditorialRun, second.raw_item_id)
    assert first.delivery_status == "pending" and first.staging_message_id is None
    assert session.get(Post, first.post_id).telegram_message_id is None
    assert first.outcome == original_outcome
    assert second.delivery_status == "sent"
    assert session.get(Post, second.post_id).telegram_message_id == 101
    assert len(calls) == 1
    # The next pass still reports the first block, but never repeats the sent post.
    assert await server_cycle.publish_pending(session.get_bind(), enabled=True) == []
    assert len(calls) == 1
