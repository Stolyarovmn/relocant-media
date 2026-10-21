from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_editorial_integration import prepare_service
from test_editorial_package import bind_context, package_payload, parse

from relocant_media.ai.package import draft_sha256
from relocant_media.domain.dedup import event_key
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import DeliveryBlockedError, _candidate, _snapshot_member
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def reviewed(tmp_path, request, monkeypatch):
    public = getattr(request, "param", "test") == "public"
    if public:
        monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    path = tmp_path / "editorial.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        _, _, item_id = prepare_service(session)
        item = session.get(RawItem, item_id)
        source = session.get(Source, item.source_id)
        payload = package_payload()
        payload.update(version=2, policy_version="subscription-editorial-v2", raw_item_id=item_id)
        if not public:
            payload["outcome"]["draft"]["title"] = (
                "[тестовый запуск] " + payload["outcome"]["draft"]["title"])
        payload["event"] = {
            "event_key": event_key(_candidate(item)),
            "editorial_rule_version": "belgrade-editorial-v2",
            "documents": [_snapshot_member(item, source)],
        }
        draft = parse({**payload, "reviews": payload["reviews"]}).outcome.draft
        for review in payload["reviews"]:
            review["reviewed_at"] = datetime.now(UTC).isoformat()
            review["draft_sha256"] = draft_sha256(draft)
        bind_context(payload)
        run = import_package(session, parse(payload))
        yield session, run
    engine.dispose()


class FakePublisher:
    def __init__(self, result=42, error=None):
        self.calls = []
        self.result = result
        self.error = error

    async def publish(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.result


async def send(session, run, publisher, **kwargs):
    return await deliver_reviewed(session, run.raw_item_id, publisher,
                                  target_channel_id=APPROVED_CHANNEL_ID, enabled=True, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewed", ["public"], indirect=True)
async def test_public_reviewed_package_delivery_and_at_most_once(reviewed):
    session, run = reviewed
    publisher = FakePublisher()
    result = await send(session, run, publisher)
    assert result.message_id == 42
    assert not publisher.calls[0].startswith("[тестовый запуск]")
    await send(session, run, publisher)
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_legacy_pending_package_is_blocked_after_public_cutover(reviewed, monkeypatch):
    session, run = reviewed
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    publisher = FakePublisher()
    with pytest.raises(DeliveryBlockedError, match="presentation"):
        await send(session, run, publisher)
    assert not publisher.calls


@pytest.mark.asyncio
async def test_channel_receipt_and_duplicate_without_network(reviewed):
    session, run = reviewed

    class DurablePublisher(FakePublisher):
        async def publish(self, text):
            with Session(session.get_bind()) as other:
                assert other.get(EditorialRun, run.raw_item_id).delivery_status == "sending"
            return await super().publish(text)

    publisher = DurablePublisher()
    result = await send(session, run, publisher)
    post = session.get(Post, run.post_id)
    assert result.message_id == post.telegram_message_id == 42
    assert post.published_at.tzinfo is not None
    assert run.delivery_status == "sent" and run.staging_message_id is None
    again = await send(session, run, publisher)
    assert again.message_id == 42 and len(publisher.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("secret"), KeyboardInterrupt(), 0, True, "42"])
async def test_unknown_outcome_never_retries(reviewed, failure):
    session, run = reviewed
    publisher = (FakePublisher(error=failure) if isinstance(failure, BaseException)
                 else FakePublisher(result=failure))
    expected = type(failure) if isinstance(failure, BaseException) else DeliveryBlockedError
    with pytest.raises(expected):
        await send(session, run, publisher)
    session.refresh(run)
    assert run.delivery_status == "uncertain"
    assert session.get(Post, run.post_id).telegram_message_id is None
    with pytest.raises(DeliveryBlockedError, match="reconciliation"):
        await send(session, run, publisher)
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["package", "prefix", "evidence", "sending", "staging"])
async def test_channel_guards_before_network(reviewed, mutation):
    session, run = reviewed
    if mutation == "package":
        run.outcome = {key: value for key, value in run.outcome.items() if key != "_package"}
    elif mutation == "prefix":
        session.get(Post, run.post_id).title = "No prefix"
    elif mutation == "evidence":
        session.get(RawItem, run.raw_item_id).body += " changed"
    elif mutation == "staging":
        run.staging_message_id = 99
    else:
        run.delivery_status = "sending"
    session.commit()
    publisher = FakePublisher()
    with pytest.raises(DeliveryBlockedError):
        await send(session, run, publisher)
    assert publisher.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("target,enabled", [(APPROVED_CHANNEL_ID, False), (-100123, True),
                                           (str(APPROVED_CHANNEL_ID), True)])
async def test_explicit_enable_and_numeric_target_required(reviewed, target, enabled):
    session, run = reviewed
    publisher = FakePublisher()
    with pytest.raises(DeliveryBlockedError):
        await deliver_reviewed(session, run.raw_item_id, publisher,
                               target_channel_id=target, enabled=enabled)
    assert publisher.calls == []
