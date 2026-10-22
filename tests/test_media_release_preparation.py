import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_editorial_integration import prepare_service
from test_editorial_package import bind_context, package_payload, parse

from relocant_media.ai.package import draft_sha256
from relocant_media.bot.media import MediaRelease, validate_media
from relocant_media.collectors.article_media import extract_article_media_candidates
from relocant_media.domain.dedup import event_key
from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.editorial import _candidate, _snapshot_member
from relocant_media.services.media_release_preparation import (
    MediaReleaseDraft,
    prepare_media_release_bundle,
    seal_media_release_bundle,
)
from relocant_media.services.package_import import import_package
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import RawItem, Source
from relocant_media.storage.sqlite_schema import initialize_sqlite

JPEG = b"\xff\xd8\xffreviewed-photo"


def _ready_with_media(tmp_path: Path) -> tuple[object, Session, int, str]:
    database = tmp_path / "editorial.sqlite3"
    initialize_sqlite(database)
    engine = create_engine(f"sqlite:///{database}")
    session = Session(engine)
    _, _, raw_id = prepare_service(session)
    item = session.get(RawItem, raw_id)
    assert item is not None
    source = session.get(Source, item.source_id)
    assert source is not None

    payload = package_payload()
    payload.update(version=2, policy_version="subscription-editorial-v2", raw_item_id=raw_id)
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
    import_package(session, parse(payload))

    html = (
        "<article><figure><img src='/media/event.jpg' width='1200' height='800'>"
        "<figcaption>Foto: Grad Beograd</figcaption></figure></article>"
    )
    encoded = html.encode()
    candidates = [asdict(value) for value in extract_article_media_candidates(html, item.url)]
    assert len(candidates) == 1
    session.add(
        RawItemMediaSnapshot(
            raw_item_id=raw_id,
            source_page=item.url,
            source_html=html,
            source_html_sha256=hashlib.sha256(encoded).hexdigest(),
            source_html_bytes=len(encoded),
            manifest={
                "version": 1,
                "source_page": item.url,
                "source_html_sha256": hashlib.sha256(encoded).hexdigest(),
                "source_html_bytes": len(encoded),
                "approved_image_hosts": [],
                "source_fetch_reused": True,
                "candidate_extraction_network_access_performed": False,
                "generated_media_used": False,
                "publication_permitted": False,
                "candidate_extraction_status": "extracted",
                "candidates": candidates,
            },
        )
    )
    session.commit()
    return engine, session, raw_id, str(candidates[0]["url"])


def _assessment(image_url: str, source_page: str, *, reviewed: bool = True) -> CoverAssessment:
    return CoverAssessment(
        image_url=image_url,
        source_page=source_page,
        kind="official_poster",
        event_match=True,
        event_year=2026,
        visual_value=4,
        rights_method="permission",
        rights_status="reviewed" if reviewed else "needs_review",
        rights_evidence_url=urljoin(source_page, "/rights/event-poster"),
        rights_rationale="Written permission applies to this exact official event poster.",
        attribution="Grad Beograd",
        assessed_by="rights-reviewer",
    )


@pytest.mark.asyncio
async def test_prepare_then_two_reviews_seal_exact_release(tmp_path: Path) -> None:
    engine, session, raw_id, image_url = _ready_with_media(tmp_path)
    item = session.get(RawItem, raw_id)
    assert item is not None
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-length": str(len(JPEG)), "content-type": "image/jpeg"},
            content=JPEG,
        )

    bundle = tmp_path / "bundle"
    with httpx.Client(
        transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False
    ) as client:
        result = await prepare_media_release_bundle(
            session,
            raw_item_id=raw_id,
            assessments=(_assessment(image_url, item.url),),
            event_year=2026,
            output=bundle,
            client=client,
        )

    assert result["mode"] == "single_cover"
    assert result["publication_permitted"] is False
    assert calls == [image_url]
    draft = MediaReleaseDraft.model_validate_json(
        (bundle / "release-draft.json").read_bytes()
    )
    assert len(draft.assets) == 1
    assert draft.assets[0].origin == "official_poster"
    assert draft.assets[0].rights_verified is True
    assert "/rights/event-poster" in draft.assets[0].rights_basis
    assert draft.assets[0].attribution in draft.caption

    template = json.loads((bundle / "media-review-template.json").read_text(encoding="utf-8"))
    assert [value["role"] for value in template] == ["facts", "utility"]
    assert all(value["verdict"] == "hold" for value in template)
    assert all(value["flags"] == ["review_required"] for value in template)

    reviews = []
    for value in template:
        reviews.append(
            {
                **value,
                "reviewer_id": f"reviewer-{value['role']}",
                "verdict": "approve",
                "score": 90,
                "flags": [],
                "live_source_checked": True,
                "checked_at": datetime.now(UTC).isoformat(),
                "rationale": "Checked exact image, provenance, rights, caption and event match.",
            }
        )
    reviews_path = tmp_path / "reviews.json"
    reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
    sealed = seal_media_release_bundle(bundle=bundle, reviews_path=reviews_path)

    assert sealed["state"] == "sealed"
    assert sealed["telegram_io_performed"] is False
    release = MediaRelease.model_validate_json((bundle / "release.json").read_bytes())
    assert {review.role for review in release.reviews} == {"facts", "utility"}
    assert len(validate_media(release, bundle / "assets")) == 1
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_unreviewed_rights_fall_back_to_text_without_download(tmp_path: Path) -> None:
    engine, session, raw_id, image_url = _ready_with_media(tmp_path)
    item = session.get(RawItem, raw_id)
    assert item is not None
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=JPEG)

    bundle = tmp_path / "bundle"
    with httpx.Client(
        transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False
    ) as client:
        result = await prepare_media_release_bundle(
            session,
            raw_item_id=raw_id,
            assessments=(_assessment(image_url, item.url, reviewed=False),),
            event_year=2026,
            output=bundle,
            client=client,
        )

    assert result["mode"] == "text"
    assert result["assets_downloaded"] == 0
    assert calls == 0
    assert not bundle.exists()
    session.close()
    engine.dispose()


@pytest.mark.asyncio
async def test_seal_rejects_missing_second_review_and_changed_asset(tmp_path: Path) -> None:
    engine, session, raw_id, image_url = _ready_with_media(tmp_path)
    item = session.get(RawItem, raw_id)
    assert item is not None
    bundle = tmp_path / "bundle"

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=JPEG)

    with httpx.Client(
        transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False
    ) as client:
        await prepare_media_release_bundle(
            session,
            raw_item_id=raw_id,
            assessments=(_assessment(image_url, item.url),),
            event_year=2026,
            output=bundle,
            client=client,
        )

    template = json.loads((bundle / "media-review-template.json").read_text(encoding="utf-8"))
    one_review = tmp_path / "one-review.json"
    one_review.write_text(json.dumps([template[0]]), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly two"):
        seal_media_release_bundle(bundle=bundle, reviews_path=one_review)

    asset = next((bundle / "assets").iterdir())
    asset.write_bytes(b"changed")
    reviews = []
    for value in template:
        reviews.append(
            {
                **value,
                "reviewer_id": f"reviewer-{value['role']}",
                "verdict": "approve",
                "score": 90,
                "flags": [],
                "live_source_checked": True,
                "checked_at": datetime.now(UTC).isoformat(),
                "rationale": "Independent review complete.",
            }
        )
    reviews_path = tmp_path / "reviews.json"
    reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
    with pytest.raises(ValueError, match="changed after review"):
        seal_media_release_bundle(bundle=bundle, reviews_path=reviews_path)
    assert not (bundle / "release.json").exists()
    session.close()
    engine.dispose()
