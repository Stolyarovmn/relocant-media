from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.collectors.bvk_planned_works import (
    BvkPlannedWorksCollector,
    parse_bvk_planned_works,
)
from relocant_media.ingestion.persistence import sync_source, upsert_raw_items
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import Base, RawItem

FIXTURE = Path(__file__).parent / "fixtures" / "bvk_planned_works.html"
SOURCE_PATH = Path("cities/belgrade/sources/bvk_planned_works.yaml")


def test_parser_splits_shared_page_into_independent_notices() -> None:
    source = load_source_file(SOURCE_PATH)
    items = parse_bvk_planned_works(FIXTURE.read_text(encoding="utf-8"), source)

    assert len(items) == 3
    assert all(item.url == "https://www.bvk.rs/planirani-radovi/" for item in items)
    assert all(item.published_at is None for item in items)
    assert len({item.external_id for item in items}) == 3
    assert len({item.canonical_url for item in items}) == 3
    assert all("__bvk_notice=" in (item.canonical_url or "") for item in items)
    assert "Митковом кладенцу" in items[1].body
    assert "Ливадској" in items[1].body
    assert "footer-like" not in items[-1].body
    assert "Сервисни центар" not in items[-1].body


def test_body_change_keeps_stable_notice_identity_for_revision_detection() -> None:
    source = load_source_file(SOURCE_PATH)
    html = FIXTURE.read_text(encoding="utf-8")
    original = parse_bvk_planned_works(html, source)
    changed = parse_bvk_planned_works(
        html.replace("умањен притисак", "умањен притисак до 23 сата"),
        source,
    )

    assert changed[1].external_id == original[1].external_id
    assert changed[1].canonical_url == original[1].canonical_url
    assert changed[1].body != original[1].body


def test_structured_canonical_identity_is_persisted_without_changing_public_url() -> None:
    source = load_source_file(SOURCE_PATH)
    items = parse_bvk_planned_works(FIXTURE.read_text(encoding="utf-8"), source)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", source)
        session.flush()
        upsert_raw_items(session, items)
        session.commit()
        rows = session.scalars(select(RawItem).order_by(RawItem.id)).all()
        assert len(rows) == 3
        assert len({row.canonical_url for row in rows}) == 3
        assert all(row.url == str(source.url) for row in rows)
        assert all(row.published_at is None for row in rows)
    engine.dispose()


@pytest.mark.asyncio
async def test_collector_uses_live_response_url_and_fails_closed_on_missing_structure() -> None:
    source = load_source_file(SOURCE_PATH)
    fixture = FIXTURE.read_text(encoding="utf-8")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=fixture, request=request)
        )
    ) as client:
        items = await BvkPlannedWorksCollector(source, client).collect()
    assert len(items) == 3

    with pytest.raises(ValueError, match="heading not found"):
        parse_bvk_planned_works("<html><body>maintenance</body></html>", source)
