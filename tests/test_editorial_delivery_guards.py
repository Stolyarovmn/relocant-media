import asyncio

import pytest
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service

from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.services.editorial import DeliveryBlockedError, EditorialService
from relocant_media.storage.models import Post, RawItem, Source

editorial_session = _editorial_session


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["body", "title", "url"])
async def test_changed_evidence_blocks_delivery(editorial_session: Session, field: str) -> None:
    service, _, item_id = prepare_service(editorial_session)
    service.prepare(item_id)
    item = editorial_session.get(RawItem, item_id)
    assert item is not None
    setattr(item, field, "changed")
    editorial_session.commit()
    with pytest.raises(DeliveryBlockedError, match="evidence changed"):
        await service.deliver(item_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["title", "body"])
async def test_edited_post_cannot_send_previously_verified_draft(
    editorial_session: Session, field: str,
) -> None:
    service, _, item_id = prepare_service(editorial_session)
    run = service.prepare(item_id)
    post = editorial_session.get(Post, run.post_id)
    assert post is not None
    setattr(post, field, getattr(post, field) + " edited after verification")
    editorial_session.commit()

    class MustNotPublish:
        async def publish(self, text: str) -> int:
            raise AssertionError("changed draft must be blocked before publisher invocation")

    with pytest.raises(DeliveryBlockedError, match="post draft changed"):
        await service.deliver(item_id, MustNotPublish(), dry_run=False)
    editorial_session.refresh(run)
    assert run.delivery_status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "city", "post_status"])
async def test_changed_policy_context_blocks_delivery(
    editorial_session: Session, change: str,
) -> None:
    service, _, item_id = prepare_service(editorial_session)
    run = service.prepare(item_id)
    item = editorial_session.get(RawItem, item_id)
    assert item is not None
    source = editorial_session.get(Source, item.source_id)
    assert source is not None
    if change == "disabled":
        source.enabled = False
    elif change == "city":
        source.city = "other_city"
    else:
        post = editorial_session.get(Post, run.post_id)
        assert post is not None
        post.status = "human_review"
    editorial_session.commit()
    with pytest.raises(DeliveryBlockedError):
        await service.deliver(item_id)


@pytest.mark.asyncio
async def test_overlapping_delivery_has_one_external_request(editorial_session: Session) -> None:
    service, provider, item_id = prepare_service(editorial_session)
    service.prepare(item_id)
    started, release = asyncio.Event(), asyncio.Event()

    class SlowPublisher:
        calls = 0

        async def publish(self, text: str) -> int:
            self.calls += 1
            started.set()
            await release.wait()
            return 17

    publisher = SlowPublisher()
    first = asyncio.create_task(service.deliver(item_id, publisher, dry_run=False))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with Session(editorial_session.get_bind()) as second_session:
            second = EditorialService(second_session, EditorialPipeline(provider))
            with pytest.raises(DeliveryBlockedError, match="manual reconciliation"):
                await second.deliver(item_id, publisher, dry_run=False)
    finally:
        release.set()
        await first
    assert publisher.calls == 1
