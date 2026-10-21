import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.ingestion.persistence import (
    ReviewedEvidenceChangedError,
    capture_collected_batch,
    record_collector_failure,
    sync_source,
    upsert_raw_items,
)
from relocant_media.ingestion.runner import ingest_city
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import (
    CollectorFailure,
    EditorialRun,
    Post,
    RawItem,
    Source,
    SourceRevision,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_PATH = Path("cities/belgrade/sources/city_beograd_news.yaml")
RESET_INGESTION_TABLES = text(
    "TRUNCATE collector_failure, raw_item, source RESTART IDENTITY CASCADE"
)


def _test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    return value


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(_test_database_url(), pool_pre_ping=True)
    with Session(engine) as session:
        session.execute(RESET_INGESTION_TABLES)
        session.commit()
        yield session
        session.rollback()
        session.execute(RESET_INGESTION_TABLES)
        session.commit()
    engine.dispose()


def test_raw_item_upsert_is_idempotent_and_updates_evidence(db_session: Session) -> None:
    source = load_source_file(SOURCE_PATH)
    sync_source(db_session, "belgrade", source)
    db_session.commit()

    first = CollectedRawItem(
        source_id=source.id,
        external_id="116232",
        url="https://example.test/article",
        title="Old title",
        body="Old body",
        published_at=None,
    )
    second = CollectedRawItem(
        source_id=source.id,
        external_id="116232",
        url="https://example.test/article",
        title="Updated title",
        body="Updated body",
        published_at=None,
    )

    upsert_raw_items(db_session, [first])
    upsert_raw_items(db_session, [second])
    db_session.commit()

    assert db_session.scalar(select(func.count()).select_from(RawItem)) == 1
    stored = db_session.scalar(select(RawItem))
    assert stored is not None
    assert stored.title == "Updated title"
    assert stored.body == "Updated body"


def test_reviewed_evidence_change_is_preserved_and_explicitly_blocks_post(
    db_session: Session,
) -> None:
    source = load_source_file(SOURCE_PATH)
    sync_source(db_session, "belgrade", source)
    item = CollectedRawItem(source.id, "reviewed", "https://example.test/reviewed",
                            "Original", "Original body", None)
    upsert_raw_items(db_session, [item])
    db_session.flush()
    raw_id = db_session.scalar(select(RawItem.id))
    post = Post(city="belgrade", category="events", status="ready", title="Русский",
                body="Проверенный текст", confidence=0.96)
    db_session.add(post)
    db_session.flush()
    run = EditorialRun(raw_item_id=raw_id, post_id=post.id,
                       outcome={"status": "ready", "draft": {"body": "Проверенный текст"}},
                       evidence={"body": "Original body"}, delivery_status="pending")
    db_session.add(run)
    db_session.commit()
    # An unchanged fetch remains idempotent even after review.
    upsert_raw_items(db_session, [item])
    db_session.commit()
    changed = CollectedRawItem(source.id, "reviewed", item.url, item.title, "Changed", None)
    with pytest.raises(ReviewedEvidenceChangedError) as caught:
        upsert_raw_items(db_session, [changed])
    db_session.rollback()
    record_collector_failure(db_session, source, caught.value)
    db_session.commit()
    assert db_session.get(RawItem, raw_id).body == "Original body"
    assert run.evidence == {"body": "Original body"}
    assert run.outcome["draft"]["body"] == "Проверенный текст"
    assert run.outcome["reason"] == "source_changed_requires_review"
    assert post.status == "human_review"
    assert run.delivery_status == "pending" and run.staging_message_id is None
    assert db_session.scalar(select(func.count()).select_from(CollectorFailure)) == 1


@pytest.mark.parametrize("secondary", [False, True])
def test_reviewed_changes_preserve_evidence_and_receipts(
    db_session: Session, secondary: bool,
) -> None:
    from datetime import UTC, datetime

    source = load_source_file(SOURCE_PATH)
    sync_source(db_session, "belgrade", source)
    original = CollectedRawItem(source.id, "changed", "https://example.test/changed",
                                "Original", "Original body", None)
    anchor = CollectedRawItem(source.id, "anchor", "https://example.test/anchor",
                              "Anchor", "Anchor body", None)
    upsert_raw_items(db_session, [original, anchor])
    db_session.flush()
    raws = {raw.external_id: raw for raw in db_session.scalars(select(RawItem))}
    item = raws["changed"]
    post = Post(city="belgrade", category="events", status="published", title="Пост",
                body="Проверено", confidence=0.99, telegram_message_id=42,
                published_at=datetime.now(UTC))
    db_session.add(post)
    db_session.flush()
    run = EditorialRun(raw_item_id=raws["anchor"].id if secondary else item.id,
                       post_id=post.id, outcome={"status": "ready"},
                       evidence={"documents": [{"raw_item_id": item.id, "body": item.body}]},
                       delivery_status="sent", staging_message_id=42)
    db_session.add(run)
    db_session.commit()
    changed = CollectedRawItem(source.id, original.external_id, original.url,
                               original.title, "New body", None)
    later = CollectedRawItem(source.id, "later", "https://example.test/later",
                             "Other news", "Useful new article", None)
    assert capture_collected_batch(db_session, [changed, later]) == (1, 1)
    db_session.commit()
    revision = db_session.scalar(select(SourceRevision))
    assert revision is not None and revision.evidence["body"] == "New body"
    assert revision.raw_item_id == item.id
    assert item.body == "Original body"
    assert run.evidence["documents"][0]["body"] == "Original body"
    # A sent run is immutable history. The revision is handled as a separate follow-up
    # candidate instead of retroactively changing the outcome that produced the receipt.
    assert run.outcome == {"status": "ready"}
    assert (run.delivery_status, run.staging_message_id, run.post_id) == ("sent", 42, post.id)
    assert post.status == "published" and post.telegram_message_id == 42
    assert db_session.scalar(select(func.count()).select_from(RawItem)) == 3
    # Repeated observations are visible as holds, without duplicate revision rows.
    assert capture_collected_batch(db_session, [changed, later]) == (1, 1)
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(SourceRevision)) == 1
    changed_again = CollectedRawItem(source.id, original.external_id, original.url,
                                     original.title, "Third version", None)
    assert capture_collected_batch(db_session, [changed_again]) == (0, 1)
    db_session.commit()
    assert db_session.scalar(select(func.count()).select_from(SourceRevision)) == 2


def test_revision_capture_rolls_back_atomically(db_session: Session) -> None:
    source = load_source_file(SOURCE_PATH)
    sync_source(db_session, "belgrade", source)
    item = CollectedRawItem(source.id, "rollback", "https://example.test/r", "Title", "Old", None)
    upsert_raw_items(db_session, [item])
    db_session.flush()
    raw = db_session.scalar(select(RawItem))
    run = EditorialRun(raw_item_id=raw.id, outcome={"status": "ready"}, evidence={"body": "Old"})
    db_session.add(run)
    db_session.commit()
    change = CollectedRawItem(source.id, item.external_id, item.url, item.title, "New", None)
    capture_collected_batch(db_session, [change])
    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(SourceRevision)) == 0
    db_session.refresh(run)
    assert run.outcome["status"] == "ready"


def test_ingest_city_runs_active_strategy_and_stays_idempotent(
    db_session: Session,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    (source_dir / "city_news.yaml").write_text(
        """id: city_news_test
name: Test City News
publisher: Test City
ownership: official
url: https://www.beograd.rs/lat/vesti
language: sr-Latn
source_type: official_city_news
trust: 1.0
check_every: 10m
categories: [city]
extraction:
  strategy: beograd_news_list
  status: active
verification:
  checked_at: 2026-10-18
""",
        encoding="utf-8",
    )
    # An unselected invalid strategy must not be synced, fetched, or recorded as failure.
    original = (source_dir / "city_news.yaml").read_text(encoding="utf-8")
    (source_dir / "unselected.yaml").write_text(
        original.replace("city_news_test", "unselected").replace(
            "beograd_news_list", "unsupported",
        ), encoding="utf-8",
    )
    html = (FIXTURES / "beograd_news_list.html").read_text(encoding="utf-8")
    article = (FIXTURES / "beograd_article.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        content = html if request.url.path == "/lat/vesti" else article
        return httpx.Response(200, text=content, request=request)

    async def run_twice() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await ingest_city(
                "belgrade", source_dir, db_session, client, source_ids=("city_news_test",),
            )
            second = await ingest_city(
                "belgrade", source_dir, db_session, client, source_ids=("city_news_test",),
            )
        assert first.raw_items_written == 2
        assert second.raw_items_written == 2
        assert first.failures == 0
        assert second.failures == 0

    import asyncio

    asyncio.run(run_twice())

    assert db_session.scalar(select(func.count()).select_from(Source)) == 1
    assert db_session.scalar(select(func.count()).select_from(RawItem)) == 2
    rows = db_session.scalars(select(RawItem)).all()
    assert all(row.body.startswith("Prvi pasus probnog sadržaja vesti.") for row in rows)


@pytest.mark.parametrize("source_ids", [(), ("not_registered",)])
def test_ingestion_rejects_unknown_or_empty_selection_before_database(
    source_ids: tuple[str, ...],
) -> None:
    import asyncio
    from unittest.mock import Mock

    session = Mock(spec=Session)

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="known source ids"):
                await ingest_city(
                    "belgrade", Path("cities/belgrade/sources"), session, client,
                    source_ids=source_ids,
                )
    asyncio.run(run())
    session.commit.assert_not_called()
