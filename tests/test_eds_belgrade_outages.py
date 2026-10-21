from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.collectors.eds_belgrade_outages import (
    EdsBelgradeOutagesCollector,
    parse_eds_belgrade_outages,
)
from relocant_media.ingestion.persistence import sync_source, upsert_raw_items
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import Base, RawItem

FIXTURE = Path(__file__).parent / "fixtures" / "eds_belgrade_outages.html"
EMPTY_FIXTURE = Path(__file__).parent / "fixtures" / "eds_belgrade_outages_empty.html"
SOURCE_PATH = Path("cities/belgrade/sources/eds_belgrade_outages_tomorrow.yaml")


def test_parser_extracts_independent_rows_and_explicit_schedule_date() -> None:
    source = load_source_file(SOURCE_PATH)
    items = parse_eds_belgrade_outages(FIXTURE.read_text(encoding="utf-8"), source)

    assert len(items) == 3
    assert all(item.url == str(source.url) for item in items)
    assert all(item.published_at is None for item in items)
    assert all(item.title.startswith("2026-10-22 — ") for item in items)
    assert len({item.external_id for item in items}) == 3
    assert len({item.canonical_url for item in items}) == 3
    assert all("__eds_notice=" in (item.canonical_url or "") for item in items)
    assert items[0].body == "БЈЕЛАНОВИЋЕВА: 2,"
    assert items[1].external_id != items[2].external_id


def test_street_text_change_keeps_stable_identity_for_revision_detection() -> None:
    source = load_source_file(SOURCE_PATH)
    html = FIXTURE.read_text(encoding="utf-8")
    original = parse_eds_belgrade_outages(html, source)
    changed = parse_eds_belgrade_outages(
        html.replace("БЈЕЛАНОВИЋЕВА: 2,", "БЈЕЛАНОВИЋЕВА: 2-6,"),
        source,
    )

    assert changed[0].external_id == original[0].external_id
    assert changed[0].canonical_url == original[0].canonical_url
    assert changed[0].body != original[0].body


def test_valid_empty_schedule_returns_no_items_but_broken_structure_fails() -> None:
    source = load_source_file(SOURCE_PATH)
    assert parse_eds_belgrade_outages(
        EMPTY_FIXTURE.read_text(encoding="utf-8"), source
    ) == []

    with pytest.raises(ValueError, match="schedule date not found"):
        parse_eds_belgrade_outages("<html><table></table></html>", source)
    with pytest.raises(ValueError, match="table header not found"):
        parse_eds_belgrade_outages(
            "<html><body>Планирана искључења за датум: 2026-10-22"
            "<table><tr><td>unexpected</td></tr></table></body></html>",
            source,
        )


def test_internal_identity_is_persisted_without_changing_public_url() -> None:
    source = load_source_file(SOURCE_PATH)
    items = parse_eds_belgrade_outages(FIXTURE.read_text(encoding="utf-8"), source)
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
async def test_collector_uses_response_url_and_structured_parser() -> None:
    source = load_source_file(SOURCE_PATH)
    fixture = FIXTURE.read_text(encoding="utf-8")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=fixture, request=request)
        )
    ) as client:
        items = await EdsBelgradeOutagesCollector(source, client).collect()

    assert len(items) == 3
    assert all(item.source_id == source.id for item in items)
