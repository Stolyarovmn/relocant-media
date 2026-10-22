import hashlib
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.ingestion.media_provenance import (
    MAX_SOURCE_HTML_BYTES,
    build_media_provenance,
)
from relocant_media.ingestion.persistence import sync_source, upsert_raw_items
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, EditorialRun, RawItem

FIXTURE = Path(__file__).parent / "fixtures" / "article_media_generic.html"
SOURCE_PATH = Path("cities/belgrade/sources/city_beograd_news.yaml")
SOURCE_PAGE = "https://news.official.example/articles/42"


def _item(html: str) -> CollectedRawItem:
    return CollectedRawItem(
        source_id="city_beograd_news",
        external_id="42",
        url=SOURCE_PAGE,
        title="Official update",
        body="Verified factual body",
        published_at=None,
        source_html=html,
        approved_image_hosts=("media.official.example",),
    )


def test_manifest_uses_saved_html_without_granting_media_rights() -> None:
    html = FIXTURE.read_text(encoding="utf-8")

    saved_html, manifest = build_media_provenance(_item(html))

    assert saved_html == html
    assert manifest is not None
    assert manifest["source_html_saved"] is True
    assert manifest["source_html_sha256"] == hashlib.sha256(html.encode()).hexdigest()
    assert manifest["candidate_extraction_network_access_performed"] is False
    assert manifest["publication_permitted"] is False
    assert manifest["generated_media_used"] is False
    assert manifest["requires_independent_media_reviews"] is True
    assert manifest["mode"] == "needs_visual_review"
    assert len(manifest["candidates"]) == 4
    assert all(candidate["event_match"] == "unreviewed" for candidate in manifest["candidates"])
    assert "rights_verified" not in str(manifest)


def test_oversized_html_falls_back_to_text_without_candidates() -> None:
    item = _item("x" * (MAX_SOURCE_HTML_BYTES + 1))

    saved_html, manifest = build_media_provenance(item)

    assert saved_html is None
    assert manifest is not None
    assert manifest["source_html_saved"] is False
    assert manifest["candidate_extraction_status"] == "omitted_oversize"
    assert manifest["candidates"] == []
    assert manifest["requires_independent_media_reviews"] is False
    assert manifest["mode"] == "text"


def test_media_snapshot_is_frozen_after_editorial_reference() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    first_html = FIXTURE.read_text(encoding="utf-8")
    second_html = first_html.replace("article-photo.jpg", "changed-photo.jpg")

    with Session(engine) as session:
        sync_source(session, "belgrade", load_source_file(SOURCE_PATH))
        session.commit()
        upsert_raw_items(session, [_item(first_html)])
        session.commit()

        raw = session.scalar(select(RawItem))
        assert raw is not None
        snapshot = session.get(RawItemMediaSnapshot, raw.id)
        assert snapshot is not None
        original_hash = snapshot.source_html_sha256
        assert snapshot.source_html == first_html

        session.add(
            EditorialRun(
                raw_item_id=raw.id,
                outcome={"status": "human_review"},
                evidence={"documents": [{"raw_item_id": raw.id}]},
                delivery_status="pending",
            )
        )
        session.commit()

        upsert_raw_items(session, [_item(second_html)])
        session.commit()
        session.expire_all()

        frozen = session.get(RawItemMediaSnapshot, raw.id)
        assert frozen is not None
        assert frozen.source_html_sha256 == original_hash
        assert frozen.source_html == first_html

    engine.dispose()
