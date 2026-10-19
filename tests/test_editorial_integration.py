import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from test_editorial_pipeline import (
    FakeProvider,
    _claims,
    _document,
    _draft,
    _ranking,
    _verification,
)

from relocant_media.ai.models import ClaimSet, EvidenceCitation, ExtractedClaim
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.publisher import StagingTelegramPublisher
from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.collectors.beograd_article import BelgradeArticleCollector
from relocant_media.ingestion.persistence import sync_source, upsert_raw_items
from relocant_media.services.editorial import DeliveryBlockedError, EditorialService
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


@pytest.fixture
def editorial_session() -> Iterator[Session]:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(url)
    reset = text("TRUNCATE editorial_run, post, raw_item, source RESTART IDENTITY CASCADE")
    with Session(engine) as session:
        session.execute(reset)
        session.commit()
        yield session
        session.rollback()
        session.execute(reset)
        session.commit()
    engine.dispose()


def prepare_service(session: Session, category: str = "utilities", supported: bool = True
                    ) -> tuple[EditorialService, FakeProvider, int]:
    source = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
    sync_source(session, "belgrade", source)
    document = _document()
    upsert_raw_items(session, [CollectedItem(
        source_id=source.id, external_id="editorial-test", url=document.url,
        title=document.title, body=document.body, published_at=None,
    )])
    session.commit()
    item_id = session.scalar(select(RawItem.id))
    assert item_id is not None
    provider = FakeProvider({
        "rank": _ranking(category), "claims": _claims(),
        "verify": _verification(supported=supported), "draft": _draft(),
    })
    return EditorialService(session, EditorialPipeline(provider)), provider, item_id


@pytest.mark.asyncio
async def test_db_to_structured_ai_to_fake_bot_api_is_durable_and_idempotent(
    editorial_session: Session,
) -> None:
    service, provider, item_id = prepare_service(editorial_session)
    run = service.prepare(item_id)
    assert run.outcome["status"] == "ready"
    assert run.evidence["url"] == _document().url
    assert run.outcome["claims"]["claims"][0]["citations"][0]["quote"] == _document().body
    service.prepare(item_id)
    assert provider.calls == ["rank", "claims", "verify", "draft"]
    preview = await service.deliver(item_id)
    assert preview.status == "dry_run"
    editorial_session.refresh(run)
    assert run.delivery_status == "pending"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = StagingTelegramPublisher(TelegramBotAPI("fake-token", client), "-100123")
        sent = await service.deliver(item_id, publisher, dry_run=False)
        again = await service.deliver(item_id, publisher, dry_run=False)
    assert sent.message_id == again.message_id == 42
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {"chat_id": "-100123", "text": preview.text}
    assert editorial_session.scalar(select(func.count()).select_from(EditorialRun)) == 1
    post = editorial_session.scalar(select(Post))
    assert post is not None
    assert post.telegram_message_id is None and post.published_at is None


def test_direct_review_rejects_same_event_from_second_source(
    editorial_session: Session,
) -> None:
    service, provider, item_id = prepare_service(editorial_session)
    original = editorial_session.get(RawItem, item_id)
    assert original is not None
    original.published_at = datetime(2026, 10, 19, 12, tzinfo=UTC)
    editorial_session.commit()
    service.prepare(item_id)

    editorial_session.add(
        Source(
            id="second-source",
            city="belgrade",
            name="Second source",
            url="https://media.example",
            source_type="media",
            trust=1,
            ownership="media",
            enabled=True,
        )
    )
    duplicate = RawItem(
        source_id="second-source",
        external_id="same-event",
        url="https://media.example/same-event",
        title="Servisna informacija",
        body="Drugi tekst o istoj servisnoj informaciji.",
        published_at=original.published_at + timedelta(hours=2),
    )
    editorial_session.add(duplicate)
    editorial_session.commit()

    with pytest.raises(ValueError, match="event already reviewed"):
        service.prepare(duplicate.id)
    assert provider.calls == ["rank", "claims", "verify", "draft"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("category", "supported", "status"), [
    ("immigration", True, "human_review"), ("utilities", False, "blocked"),
])
async def test_policy_prevents_delivery(
    editorial_session: Session, category: str, supported: bool, status: str,
) -> None:
    service, _, item_id = prepare_service(editorial_session, category, supported)
    assert service.prepare(item_id).outcome["status"] == status
    with pytest.raises(DeliveryBlockedError):
        await service.deliver(item_id)


@pytest.mark.asyncio
async def test_timeout_cannot_send_again_even_after_new_session(editorial_session: Session) -> None:
    service, provider, item_id = prepare_service(editorial_session)
    service.prepare(item_id)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("unknown delivery", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = StagingTelegramPublisher(TelegramBotAPI("fake-token", client), "-100123")
        with pytest.raises(httpx.ReadTimeout):
            await service.deliver(item_id, publisher, dry_run=False)
        with Session(editorial_session.get_bind()) as recovered:
            restarted = EditorialService(recovered, EditorialPipeline(provider))
            with pytest.raises(DeliveryBlockedError):
                await restarted.deliver(item_id, publisher, dry_run=False)
            run = recovered.get(EditorialRun, item_id)
            assert run is not None and run.delivery_status == "uncertain"
    assert calls == 1


def test_invalid_ai_output_rolls_back_without_partial_post(editorial_session: Session) -> None:
    service, provider, item_id = prepare_service(editorial_session)
    provider._responses.clear()
    with pytest.raises(KeyError):
        service.prepare(item_id)
    assert editorial_session.get(EditorialRun, item_id) is None
    assert editorial_session.scalar(select(func.count()).select_from(Post)) == 0


@pytest.mark.asyncio
async def test_official_article_fixture_to_persisted_draft(editorial_session: Session) -> None:
    source = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
    sync_source(editorial_session, "belgrade", source)
    url = "https://www.beograd.rs/lat/beoinfo-vesti/a116232/Bez-vode.html"
    html = (Path(__file__).parent / "fixtures/beograd_article.html").read_text(encoding="utf-8")

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=html),
    )) as client:
        item = await BelgradeArticleCollector(source, client).collect_url(url)
    upsert_raw_items(editorial_session, [item])
    editorial_session.commit()
    item_id = editorial_session.scalar(select(RawItem.id))
    assert item_id is not None
    claims = ClaimSet(claims=(ExtractedClaim(
        claim_id="c1", text=item.body, citations=(EvidenceCitation(
            source_id=source.id, url=url, quote=item.body,
        ),),
    ),))
    provider = FakeProvider({"rank": _ranking(), "claims": claims,
                             "verify": _verification(), "draft": _draft()})
    service = EditorialService(editorial_session, EditorialPipeline(provider))
    run = service.prepare(item_id)
    assert run.outcome["status"] == "ready"
    assert run.evidence["body"] == item.body
    assert run.evidence["published_at"] is not None
    preview = await service.deliver(item_id)
    assert url in preview.text
