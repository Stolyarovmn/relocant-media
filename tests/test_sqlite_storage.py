"""Реальная SQLite: сбор, повторный запуск и редакционная проверка без сети."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_editorial_integration import prepare_service
from test_ingestion_persistence import (
    test_ingest_city_runs_active_strategy_and_stays_idempotent as _check_ingestion,
)

from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.ingestion.persistence import ReviewedEvidenceChangedError, upsert_raw_items
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem


@pytest.fixture
def sqlite_session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{(tmp_path / 'editorial.sqlite3').as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_sqlite_ingestion_is_idempotent(sqlite_session: Session, tmp_path: Path) -> None:
    _check_ingestion(sqlite_session, tmp_path)


@pytest.mark.asyncio
async def test_sqlite_editorial_persists_and_reopens(sqlite_session: Session) -> None:
    service, provider, item_id = prepare_service(sqlite_session)
    item = sqlite_session.get(RawItem, item_id)
    assert item is not None
    item.published_at = datetime(2026, 10, 18, 12, tzinfo=UTC)
    sqlite_session.commit()
    run = service.prepare(item_id)
    assert run.outcome["status"] == "ready"
    assert run.evidence["url"] == item.url
    assert run.post_id is not None
    preview = await service.deliver(item_id)
    assert preview.status == "dry_run"
    with Session(sqlite_session.get_bind()) as reopened:
        saved = reopened.get(EditorialRun, item_id)
        assert saved is not None and saved.outcome == run.outcome
        assert reopened.scalar(select(func.count()).select_from(Post)) == 1
        # SQLite reloads naive UTC; an equivalent aware timestamp must remain unchanged.
        same = CollectedItem(item.source_id, item.external_id, item.url, item.title,
                             item.body, datetime(2026, 10, 18, 15,
                                                 tzinfo=timezone(timedelta(hours=3))))
        upsert_raw_items(reopened, [same])
        reopened.commit()
        changed = CollectedItem(item.source_id, item.external_id, item.url, item.title,
                                "Изменённый источник", same.published_at)
        with pytest.raises(ReviewedEvidenceChangedError):
            upsert_raw_items(reopened, [changed])
        reopened.rollback()
    service.prepare(item_id)
    assert provider.calls == ["rank", "claims", "verify", "draft"]


def test_sqlite_enforces_foreign_keys(sqlite_session: Session) -> None:
    assert sqlite_session.scalar(text("PRAGMA foreign_keys")) == 1
    sqlite_session.add(RawItem(source_id="missing", external_id="1", url="https://test.invalid",
                               title="Тест", body="Тест"))
    with pytest.raises(IntegrityError):
        sqlite_session.commit()
    sqlite_session.rollback()
