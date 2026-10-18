import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.ingestion.persistence import sync_source, upsert_raw_items
from relocant_media.ingestion.runner import ingest_city
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import RawItem, Source

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
url: https://example.test/lat/vesti
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
    html = (FIXTURES / "beograd_news_list.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async def run_twice() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await ingest_city("belgrade", source_dir, db_session, client)
            second = await ingest_city("belgrade", source_dir, db_session, client)
        assert first.raw_items_written == 2
        assert second.raw_items_written == 2
        assert first.failures == 0
        assert second.failures == 0

    import asyncio

    asyncio.run(run_twice())

    assert db_session.scalar(select(func.count()).select_from(Source)) == 1
    assert db_session.scalar(select(func.count()).select_from(RawItem)) == 2
