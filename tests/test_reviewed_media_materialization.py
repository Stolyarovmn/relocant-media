import hashlib
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.reviewed_media_materialization import (
    materialize_reviewed_media_assets,
)
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source

SOURCE_PAGE = "https://official.example/article/42"
IMAGE_URL = "https://cdn.official.example/media/event.jpg"


def _ready_record(
    session: Session,
    *,
    ownership: str = "official",
    approved_hosts: list[str] | None = None,
) -> int:
    hosts = ["cdn.official.example"] if approved_hosts is None else approved_hosts
    source = Source(
        id="official",
        city="belgrade",
        name="Official",
        url="https://official.example/",
        source_type="official_city",
        trust=Decimal("1.000"),
        ownership=ownership,
        high_risk=False,
        auto_publish=False,
        enabled=True,
    )
    session.add(source)
    session.flush()
    raw = RawItem(
        source_id=source.id,
        external_id="42",
        url=SOURCE_PAGE,
        canonical_url=SOURCE_PAGE,
        content_sha256="b" * 64,
        title="Official update",
        body="Verified body",
        published_at=None,
    )
    session.add(raw)
    session.flush()
    post = Post(
        city="belgrade",
        category="city",
        status="ready",
        title="Title",
        body="Body",
        confidence=Decimal("0.9000"),
    )
    session.add(post)
    session.flush()
    session.add(
        EditorialRun(
            raw_item_id=raw.id,
            outcome={"status": "ready", "_package_sha256": "a" * 64},
            evidence={"documents": [{"raw_item_id": raw.id}]},
            post_id=post.id,
            delivery_status="pending",
        )
    )
    html = (
        "<article><img src='https://cdn.official.example/media/event.jpg' "
        "width='1200' height='800' alt='Event'></article>"
    )
    encoded = html.encode()
    digest = hashlib.sha256(encoded).hexdigest()
    session.add(
        RawItemMediaSnapshot(
            raw_item_id=raw.id,
            source_page=SOURCE_PAGE,
            source_html=html,
            source_html_sha256=digest,
            source_html_bytes=len(encoded),
            manifest={
                "version": 1,
                "source_page": SOURCE_PAGE,
                "source_html_sha256": digest,
                "source_html_bytes": len(encoded),
                "approved_image_hosts": hosts,
                "candidate_extraction_network_access_performed": False,
                "generated_media_used": False,
                "publication_permitted": False,
                "candidates": [
                    {
                        "url": IMAGE_URL,
                        "source_page": SOURCE_PAGE,
                        "origin": "article_image",
                        "alt": "Event",
                        "attribution": "",
                        "provenance_basis": "source_page:article_img",
                        "rights_hint": "rights_unconfirmed",
                        "event_match": "unreviewed",
                        "declared_width": 1200,
                        "declared_height": 800,
                    }
                ],
            },
        )
    )
    session.commit()
    return raw.id


def _assessment(*, reviewed: bool = True) -> CoverAssessment:
    return CoverAssessment(
        image_url=IMAGE_URL,
        source_page=SOURCE_PAGE,
        kind="current_photo",
        event_match=True,
        event_year=2026,
        visual_value=4,
        rights_method="permission" if reviewed else "unknown",
        rights_status="reviewed" if reviewed else "needs_review",
        rights_evidence_url="https://official.example/rights/42" if reviewed else "",
        rights_rationale="Explicit permission recorded for this exact image." if reviewed else "",
        attribution="Фото: Official" if reviewed else "",
        assessed_by="rights-reviewer",
    )


def test_reviewed_official_current_photo_materializes_exact_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, content=b"\xff\xd8\xffreviewed-photo")

    output = tmp_path / "materialized"
    with Session(engine) as session, httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        raw_id = _ready_record(session)
        result = materialize_reviewed_media_assets(
            session,
            raw_item_id=raw_id,
            assessments=(_assessment(),),
            event_year=2026,
            output=output,
            client=client,
        )

    assert requests == [IMAGE_URL]
    assert result["mode"] == "single_cover"
    assert result["asset_count"] == 1
    assert result["publication_permitted"] is False
    assert result["rights_inferred"] is False
    manifest = json.loads((output / "assets.json").read_text(encoding="utf-8"))
    asset = manifest["assets"][0]
    assert asset["origin"] == "official_photo"
    assert asset["source_url"] == SOURCE_PAGE
    assert asset["rights_verified"] is True
    assert asset["attribution"] == "Фото: Official"
    assert (output / "assets" / asset["file_name"]).is_file()
    audit = json.loads((output / "materialization.json").read_text(encoding="utf-8"))
    assert audit["publication_permitted"] is False
    assert audit["independent_media_reviews_completed"] is False
    assert audit["items"][0]["reviewed_image_url"] == IMAGE_URL
    assert audit["items"][0]["approved_image_hosts"] == ["cdn.official.example"]
    engine.dispose()


def test_unreviewed_rights_stay_text_only_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    output = tmp_path / "materialized"
    with Session(engine) as session, httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        raw_id = _ready_record(session)
        result = materialize_reviewed_media_assets(
            session,
            raw_item_id=raw_id,
            assessments=(_assessment(reviewed=False),),
            event_year=2026,
            output=output,
            client=client,
        )

    assert result["mode"] == "text"
    assert result["download_performed"] is False
    assert calls == 0
    assert not output.exists()
    engine.dispose()


def test_current_photo_from_nonofficial_source_is_blocked_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"\xff\xd8\xffshould-not-download")

    output = tmp_path / "materialized"
    with Session(engine) as session, httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        raw_id = _ready_record(session, ownership="commercial_media")
        with pytest.raises(ValueError, match="explicitly official"):
            materialize_reviewed_media_assets(
                session,
                raw_item_id=raw_id,
                assessments=(_assessment(),),
                event_year=2026,
                output=output,
                client=client,
            )

    assert calls == 0
    assert not output.exists()
    engine.dispose()


def test_candidate_must_reproduce_under_saved_ingestion_host_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"\xff\xd8\xffshould-not-download")

    output = tmp_path / "materialized"
    with Session(engine) as session, httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        raw_id = _ready_record(session, approved_hosts=[])
        with pytest.raises(ValueError, match="not reproducible"):
            materialize_reviewed_media_assets(
                session,
                raw_item_id=raw_id,
                assessments=(_assessment(),),
                event_year=2026,
                output=output,
                client=client,
            )

    assert calls == 0
    assert not output.exists()
    engine.dispose()
