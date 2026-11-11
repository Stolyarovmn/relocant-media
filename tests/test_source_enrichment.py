import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.media import MediaRelease, media_context_sha256
from relocant_media.collectors.article_media import (
    ArticleMediaError,
    extract_article_media_candidates,
)
from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.domain.source_locations import (
    address_search_map,
    extract_source_maps,
    is_public_https_url,
    map_provider,
)
from relocant_media.ingestion.media_provenance import build_media_provenance
from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.media_review_packet import (
    build_media_review_packet,
    plan_reviewed_packet,
)
from relocant_media.services.production_media import prepare_production_media
from relocant_media.services.reviewed_media_materialization import materialize_reviewed_media_assets
from relocant_media.services.source_enrichment import (
    build_media_preparation_packet,
    build_source_enrichment,
    plan_illustration,
    select_source_map,
    validate_against_documents,
    validate_source_map,
)
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source


def _record(
    session: Session, city: str, address: str, country: str,
) -> tuple[int, EvidenceDocument]:
    url = f"https://{city}.official.example/article/42"
    quote = f"{address}, {city}, {country}: карта места"
    map_url = f"https://www.google.com/maps/search/?api=1&query={city}"
    html = f'<article><p><a href="{map_url}">{quote}</a></p><img src="/event.jpg"></article>'
    collected = CollectedItem(
        source_id=city, external_id="42", url=url, title="Городское событие", body=quote,
        published_at=None, source_html=html,
    )
    source_html, manifest = build_media_provenance(collected)
    assert manifest is not None
    session.add(Source(
        id=city, city=city, name=city, url=url, source_type="official_city",
        trust=Decimal("1.000"), ownership="official",
    ))
    session.flush()
    raw = RawItem(
        source_id=city, external_id="42", url=url, title=collected.title, body=quote,
        published_at=None,
    )
    session.add(raw)
    session.flush()
    session.add(RawItemMediaSnapshot(
        raw_item_id=raw.id, source_page=url, source_html=source_html,
        source_html_sha256=manifest["source_html_sha256"],
        source_html_bytes=manifest["source_html_bytes"], manifest=manifest,
    ))
    post = Post(
        city=city, category="events", status="ready", title="Событие", body=quote,
        confidence=Decimal("0.95"),
    )
    session.add(post)
    session.flush()
    session.add(EditorialRun(
        raw_item_id=raw.id, post_id=post.id,
        evidence={"documents": [{"raw_item_id": raw.id}]},
        outcome={"status": "ready", "_package_sha256": "a" * 64},
    ))
    session.commit()
    return raw.id, EvidenceDocument(
        source_id=city, url=url, title=raw.title, body=raw.body,
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


@pytest.mark.parametrize("city,address,country", [
    ("belgrade", "Knez Mihailova 6", "Serbia"),
    ("lisbon", "Rua Augusta 10", "Portugal"),
])
def test_same_pipeline_is_portable_and_deterministic(session, city, address, country) -> None:
    raw_id, document = _record(session, city, address, country)
    result = build_source_enrichment(session, raw_id)
    assert result == json.loads(json.dumps(build_source_enrichment(session, raw_id)))
    validate_against_documents(result, [document])
    selected = select_source_map(result, source_url=document.url)
    assert selected is not None
    assert selected["location_verified"] is False
    assert selected["label"] == "Карта из источника"
    assert selected["source_page"] == document.url
    snapshot = session.get(RawItemMediaSnapshot, raw_id)
    assert selected["source_html_sha256"] == snapshot.source_html_sha256
    assert len(snapshot.manifest["map_candidates"]) == 1
    assert result["network_access_performed"] is False
    assert result["documents"][0]["media_candidates"][0]["rights_hint"] == "rights_unconfirmed"
    plan = plan_illustration(result, source_url=document.url)
    assert plan["illustration_mode"] == "source_preview"
    assert plan["asset_refs"] == []
    assert plan["preview_url"] == document.url
    assert plan["attachment_status"] == "awaiting_explicit_rights_and_two_reviews"
    search = address_search_map(
        address=address, city=city, country=country, quote=document.body, document=document,
    )
    assert search["kind"] == "address_search"
    assert search["label"] == "Поиск адреса на карте"
    assert search["location_verified"] is False


def test_wrong_address_or_city_cannot_create_map() -> None:
    document = EvidenceDocument(
        source_id="lisbon", url="https://official.example/news", title="Место",
        body="Rua Augusta 10, Lisbon, Portugal",
    )
    for address, city, country in [
        ("Rua Augusta 11", "Lisbon", "Portugal"),
        ("Rua Augusta 10", "Belgrade", "Portugal"),
        ("Rua Augusta 10", "Lisbon", "Serbia"),
    ]:
        with pytest.raises(ValueError, match="exact source quote"):
            address_search_map(
                address=address, city=city, country=country, quote=document.body, document=document,
            )


@pytest.mark.parametrize("url", [
    "http://www.google.com/maps/place/test", "https://www.google.com.evil.example/maps/test",
    "https://www.google.com@evil.example/maps/test", "https://127.0.0.1/maps/test",
    "https://[::1]/maps/test", "https://169.254.169.254/latest", "file:///etc/passwd",
    "https://localhost/maps/test", "https://maps.internal/maps/test",
    "https://www.google.com:444/maps/test", "https://www.google.com/url?q=https://evil.example",
    "https://www.google.com/maps/?url=https://evil.example", "https://www.google.com/maps/%0a",
    "https://www.google.com\\@127.0.0.1/maps/test", "https://2130706433/maps/test",
])
def test_untrusted_map_urls_are_never_selected(url) -> None:
    assert map_provider(url) is None
    assert extract_source_maps(f'<article><a href="{url}">Место</a></article>',
                               "https://official.example/news") == []


def test_unknown_provider_missing_hash_and_ambiguous_locations_fail_closed(session) -> None:
    raw_id, document = _record(session, "lisbon", "Rua Augusta 10", "Portugal")
    result = build_source_enrichment(session, raw_id)
    item = result["documents"][0]
    candidate = item["map_candidates"][0]
    item["map_candidates"].append({**candidate, "url": "https://www.openstreetmap.org/node/1",
                                   "provider": "openstreetmap"})
    assert select_source_map(result) is None
    assert select_source_map(result, map_url=candidate["url"]) == candidate
    item["map_candidates"] = [{**candidate, "url": "https://evil.example/map", "provider": None}]
    assert select_source_map(result) is None
    item["map_candidates"] = [{**candidate, "source_html_sha256": None}]
    item["source_html_sha256"] = None
    assert select_source_map(result) is None
    with pytest.raises(ValueError, match="not supported"):
        validate_source_map({**candidate, "source_quote": "Другой адрес"}, [document])


def test_html_or_evidence_replacement_invalidates_enrichment(session) -> None:
    raw_id, document = _record(session, "lisbon", "Rua Augusta 10", "Portugal")
    result = build_source_enrichment(session, raw_id)
    changed = document.model_copy(update={"body": "Другая статья под прежним URL"})
    with pytest.raises(ValueError, match="exact evidence"):
        validate_against_documents(result, [changed])
    snapshot = session.get(RawItemMediaSnapshot, raw_id)
    snapshot.source_html += "<p>Изменено</p>"
    changed_result = build_source_enrichment(session, raw_id)
    assert select_source_map(changed_result) is None
    assert changed_result["documents"][0]["media_candidates"] == []
    assert changed_result["documents"][0]["reason"] == "source_snapshot_invalid"


def test_source_maps_ignore_navigation_and_unrelated_quotes(session) -> None:
    url = "https://www.google.com/maps/place/test"
    html = f'<main><nav><a href="{url}">Офис сайта</a></nav><p>Статья</p></main>'
    assert extract_source_maps(html, "https://official.example/news") == []
    raw_id, _ = _record(session, "lisbon", "Rua Augusta 10", "Portugal")
    raw = session.get(RawItem, raw_id)
    raw.body = "Статья не содержит места, указанного в HTML"
    assert select_source_map(build_source_enrichment(session, raw_id)) is None


def test_local_media_sources_and_candidates_cannot_be_download_candidates() -> None:
    assert not is_public_https_url("https://10.0.0.1/article")
    with pytest.raises(ArticleMediaError):
        extract_article_media_candidates('<article><img src="/event.jpg"></article>',
                                         "https://10.0.0.1/article")
    candidates = extract_article_media_candidates(
        '<article><img src="https://127.0.0.1/event.jpg"></article>',
        "https://official.example/article", approved_image_hosts=frozenset({"127.0.0.1"}),
    )
    assert candidates == []


def test_cover_selection_unknown_rights_falls_back_to_real_source_preview_transport(session):
    raw_id, document = _record(session, "lisbon", "Rua Augusta 10", "Portugal")
    result = build_source_enrichment(session, raw_id)
    packet = build_media_review_packet(session, raw_id)
    template = packet["candidates"][0]["assessment_template"]
    assessment = CoverAssessment.model_validate({**template, "assessed_by": "reviewer"})
    cover = plan_reviewed_packet(packet, (assessment,), event_year=2026)
    assert cover["mode"] == "text"
    plan = plan_illustration(result, source_url=document.url)
    assert plan["illustration_mode"] == "source_preview"
    calls = []

    def fake_transport(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async def deliver_without_io():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_transport)) as client:
            bot = TelegramBotAPI("fake-offline-token", client)
            return await bot.send_message(-100123, f"Событие\n\nИсточник: {document.url}")

    # MockTransport выполняется сразу, без сокетов, event loop и сетевого I/O.
    coroutine = deliver_without_io()
    with pytest.raises(StopIteration) as completed:
        coroutine.send(None)
    assert completed.value.value == 42
    assert len(calls) == 1
    assert calls[0][0].endswith("/sendMessage")
    assert calls[0][1]["link_preview_options"]["url"] == document.url
    assert plan_illustration(result, source_url=document.url,
                             allow_source_preview=False)["illustration_mode"] == "text"
    assert plan_illustration(result, source_url="https://other.example/news")[
        "illustration_mode"] == "text"


def test_reviewed_cover_selection_is_not_a_media_release(session) -> None:
    raw_id, document = _record(session, "lisbon", "Rua Augusta 10", "Portugal")
    packet = build_media_review_packet(session, raw_id)
    template = packet["candidates"][0]["assessment_template"]
    assessment = CoverAssessment.model_validate({
        **template, "assessed_by": "rights-reviewer", "kind": "official_poster",
        "event_match": True, "event_year": 2026, "visual_value": 4,
        "rights_method": "permission", "rights_status": "reviewed",
        "rights_evidence_url": document.url, "rights_rationale": "Явное разрешение на афишу",
        "attribution": "Афиша: организатор",
    })
    cover = plan_reviewed_packet(packet, (assessment,), event_year=2026)
    assert cover["mode"] == "single_cover"
    assert cover["requires_independent_media_reviews"] is True
    plan = plan_illustration(build_source_enrichment(session, raw_id), source_url=document.url)
    assert plan["illustration_mode"] == "source_preview"
    assert plan["asset_refs"] == []
    bad_rights = assessment.model_copy(update={"rights_evidence_url": "https://127.0.0.1/rights"})
    assert plan_reviewed_packet(packet, (bad_rights,), event_year=2026)["mode"] == "text"
    preparation = build_media_preparation_packet(session, raw_id)
    assert preparation["status"] == "awaiting_explicit_cover_assessments"
    assert preparation["selected_asset_refs"] == []
    assert preparation["candidate_refs"][0]["image_url"] == assessment.image_url
    assessed = build_media_preparation_packet(
        session, raw_id, assessments=(assessment,), event_year=2026,
    )
    assert assessed["status"] == "awaiting_materialization_and_two_reviews"
    assert len(assessed["selected_asset_refs"]) == 1
    assert assessed["selected_asset_refs"][0]["materialized_asset_sha256"] is None
    assert assessed["publication_permitted"] is False
    assert assessed["download_permitted"] is False
    assert assessed["next_cli_argv"][0] == "relocant-media-assets"
    with pytest.raises(ValueError, match="event year"):
        build_media_preparation_packet(session, raw_id, assessments=(assessment,))


def test_offline_full_attachment_path_preserves_two_exact_media_reviews(
    session, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    raw_id, document = _record(session, "lisbon", "Rua Augusta 10", "Portugal")
    enrichment = build_source_enrichment(session, raw_id)
    packet = build_media_preparation_packet(session, raw_id)
    assessment = CoverAssessment.model_validate({
        **packet["review_packet"]["candidates"][0]["assessment_template"],
        "assessed_by": "rights-reviewer", "kind": "official_poster", "event_match": True,
        "event_year": 2026, "visual_value": 4, "rights_method": "permission",
        "rights_status": "reviewed", "rights_evidence_url": document.url,
        "rights_rationale": "Разрешение организатора на точную афишу",
        "attribution": "Афиша: организатор",
    })
    selected = build_media_preparation_packet(
        session, raw_id, assessments=(assessment,), event_year=2026,
    )
    assert len(selected["selected_asset_refs"]) == 1
    bundle = tmp_path / str(raw_id)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"\xff\xd8\xffoffline-poster")
    )) as client:
        materialize_reviewed_media_assets(
            session, raw_item_id=raw_id, assessments=(assessment,), event_year=2026,
            output=bundle, client=client,
        )
    assets = json.loads((bundle / "assets.json").read_text(encoding="utf-8"))["assets"]
    text = "Событие"
    caption = text + "\n\n" + assessment.attribution
    now = datetime.now(UTC)
    release = MediaRelease.model_validate({
        "version": 1, "release_id": "offline-test", "raw_item_id": raw_id,
        "post_id": packet["post_id"], "editorial_package_sha256": "a" * 64,
        "target_id": "-100123", "caption": caption, "assets": assets,
        # Только тестовые аттестации; production-код их никогда не создаёт.
        "reviews": [{
            "reviewer_id": f"test-{role}", "role": role, "verdict": "approve", "score": 90,
            "flags": [], "live_source_checked": True, "checked_at": now,
            "reviewed_asset_sha256": [asset["sha256"] for asset in assets],
            "caption_sha256": hashlib.sha256(caption.encode()).hexdigest(),
            "context_sha256": "0" * 64, "rationale": "Тестовые точные байты и подпись",
        } for role in ("facts", "utility")],
    })
    release = release.model_copy(update={"reviews": tuple(
        review.model_copy(update={"context_sha256": media_context_sha256(release)})
        for review in release.reviews
    )})
    (bundle / "release.json").write_text(release.model_dump_json(), encoding="utf-8")
    arguments = {
        "raw_item_id": raw_id, "post_id": packet["post_id"],
        "editorial_package_sha256": "a" * 64, "target_channel_id": -100123,
        "editorial_text": text, "root": tmp_path, "now": now,
    }
    prepared = prepare_production_media(**arguments)
    assert prepared is not None
    plan = plan_illustration(enrichment, source_url=document.url, prepared_media=prepared)
    assert plan["illustration_mode"] == "reviewed_attachment"
    assert plan["asset_refs"][0]["sha256"] == assets[0]["sha256"]
    calls = []

    def transport(request):
        calls.append(request.url.path)
        assert b"offline-poster" in request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async def fake_delivery():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await TelegramBotAPI("fake-offline-token", client).send_photo(
                -100123, prepared.uploads[0], prepared.caption,
            )

    with pytest.raises(StopIteration) as completed:
        fake_delivery().send(None)
    assert completed.value.value == 42
    assert len(calls) == 1 and calls[0].endswith("/sendPhoto")
    duplicate_reviewer = release.model_copy(update={"reviews": (
        release.reviews[0], release.reviews[1].model_copy(
            update={"reviewer_id": release.reviews[0].reviewer_id},
        ),
    )})
    (bundle / "release.json").write_text(duplicate_reviewer.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="distinct media reviewers"):
        prepare_production_media(**arguments)
    (bundle / "release.json").write_text(release.model_dump_json(), encoding="utf-8")
    (bundle / "assets" / assets[0]["file_name"]).write_bytes(b"\xff\xd8\xffchanged")
    with pytest.raises(ValueError, match="changed after review"):
        prepare_production_media(**arguments)
