from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_utility_scope import document as power_document
from test_water_outage_scope import (
    CITY_BODY,
    CITY_URL,
    MAP_URL,
    NEWS_TITLE,
    NEWS_URL,
    map_html,
    news,
)

from relocant_media.ai.models import EvidenceDocument, RussianDraft
from relocant_media.collectors.base import RawItem
from relocant_media.collectors.beograd_article import parse_beograd_article
from relocant_media.collectors.bvk_outage_news import BvkOutageNewsCollector
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.services.editorial_products import (
    build_presentation,
    present_draft,
    validate_presentation,
    verify_stored_presentation,
)
from relocant_media.services.native_map import (
    NOTICE_COVER,
    bind_native_map_draft,
    native_message_html,
    verify_live_native_map,
)
from relocant_media.services.rich_delivery import prepare_native_transport
from relocant_media.services.utility_map import build_utility_point, utility_map_fragment
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import Base


def source(source_id):
    return load_source_file(Path("cities/belgrade/sources") / f"{source_id}.yaml")


@pytest.mark.asyncio
async def test_bvk_exact_article_confirms_date_and_official_map():
    row = {
        "id": 91008, "slug": NEWS_URL.strip("/").rsplit("/", 1)[-1], "link": NEWS_URL,
        "title": {"rendered": NEWS_TITLE}, "date_gmt": "2026-06-05T09:19:45",
    }
    page = map_html().replace("post-entry post-91008", "post-entry post-entry-91008")

    def handler(request):
        if request.url.path.endswith("/posts"):
            assert request.url.params["slug"] == row["slug"]
            return httpx.Response(200, json=[row])
        assert str(request.url) == NEWS_URL
        return httpx.Response(200, text=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        item = await BvkOutageNewsCollector(source("bvk_outage_news"), client).collect_url(NEWS_URL)
    document = EvidenceDocument.model_validate(item, from_attributes=True)
    assert document.published_at == datetime(2026, 6, 5, 9, 19, 45, tzinfo=UTC)
    point = build_utility_point(document, item.source_html)
    assert point["official_map_url"] == MAP_URL
    assert MAP_URL in utility_map_fragment(point)
    assert "image_sha256" not in point
    with pytest.raises(ValueError, match="changed in live source"):
        verify_live_native_map({"point": point}, document, page.replace("ledine-mapa", "photo"))


def test_city_power_lists_keep_only_explicit_houses_and_links_without_coordinates():
    expected = power_document("Vračar 9.00 - 11.00 VOJVODE ŠUPLJIKCA: 42, 44-48,")
    html = (f'<article><h1>{expected.title}</h1><time class="story__time">12.11.2026.</time>'
            f'<p>Najava</p><ul><li><p>{expected.body}</p></li></ul></article>')
    item = parse_beograd_article(html, source(expected.source_id), expected.url)
    assert item.body == f"Najava\n\n{expected.body}"
    document = EvidenceDocument.model_validate(item, from_attributes=True)
    point = build_utility_point(document, html, {"points": None, "lines": None})
    fragment = utility_map_fragment(point)
    assert "VOJVODE ŠUPLJIKCA 42" in fragment and "44-48" not in fragment
    assert "https://www.openstreetmap.org/search?" in fragment
    assert "<tg-map" not in fragment

    address = point["addresses"][0]
    verified = {"points": [{"address": address, "latitude": "44.798354",
                            "longitude": "20.486063", "osm_url":
                            "https://www.openstreetmap.org/way/398033226"}]}
    point = build_utility_point(document, html, verified)
    fragment = utility_map_fragment(point)
    assert '<tg-map lat="44.798354" long="20.486063"' in fragment
    assert "https://www.openstreetmap.org/way/398033226" in fragment


def test_water_segment_png_and_review_are_bound_to_immutable_snapshot():
    live = EvidenceDocument(source_id="city_beograd_news", url=CITY_URL, title="Planirani radovi",
                            body=CITY_BODY, published_at_precision="day",
                            published_at=datetime(2026, 11, 12, tzinfo=ZoneInfo("Europe/Belgrade")))
    document = live.model_copy(update={"published_at": live.published_at.astimezone(UTC)})
    html = "<article><p>" + CITY_BODY + "</p></article>"
    segment = build_utility_point(document, html)["segments"][0]
    geography = {"lines": [{"segment": segment,
                            "coordinates": [["44.7850", "20.4840"], ["44.7860", "20.4850"]],
                            "osm_urls": ["https://www.openstreetmap.org/way/123"]}]}
    item = RawItem(**live.model_dump(), external_id="116609",
                   source_html=html, utility_geography=geography)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", source(item.source_id))
        capture_collected_batch(session, [item])
        session.commit()
        profile = load_city_profile("belgrade")
        presentation = build_presentation(session, 1, (document,), document_ids=[1],
                                          profile=profile, rubric_id="city_changes")
        draft = present_draft(RussianDraft(title="Отключение воды в Вождоваце",
                                           body="Воды не будет с 8:30 до 18:00.",
                                           claim_ids=("period",)), presentation)
        presentation = bind_native_map_draft(presentation, draft)
        native = presentation.native_map
        assert native is not None
        assert "<b>Рубрика</b>: Что меняется в городе" in native["message_html"]
        assert NOTICE_COVER not in native["message_html"]
        assert "<tg-map" not in native["message_html"]
        assert "Kruševačke" in native["message_html"]
        validate_presentation(presentation, profile, (document,), draft, rubric_id="city_changes")
        verify_stored_presentation(session, 1, (document,), document_ids=[1],
                                   presentation=presentation)
        verify_live_native_map(native, live, html)
        changed = live.model_copy(update={"body": live.body.replace("Kruševačke", "Druge")})
        with pytest.raises(ValueError, match="changed in live source"):
            verify_live_native_map(native, changed, html)
        package = SimpleNamespace(presentation=presentation, outcome=SimpleNamespace(draft=draft))
        bot = SimpleNamespace(send_notice_message=lambda *args: None)
        _, prepared = prepare_native_transport(
            package, SimpleNamespace(bot=bot), None,
            editorial_text=f"{draft.title}\n\n{draft.body}\n\nИсточник: {live.url}",
        )
        assert prepared.photo is None and prepared.source_preview_url == live.url
        assert presentation.illustration["preview_url"] == live.url
    engine.dispose()


def test_notice_collapses_plain_power_addresses_with_their_source_intervals():
    evidence = power_document("\n".join([
        "Vračar 9.00 - 11.00 VOJVODE ŠUPLJIKCA: 42, 44-48, BB,",
        "Zvezdara 13.00 - 15.00 BRANKA KRSMANOVIĆA: 2,",
        "Lazarevac 09.00 - 13.00 Naselje JUNKOVAC (zaseok BELAĆEVIĆI)",
        "Novi Beograd 09.00 - 13.00 celo naselje BEŽANIJSKA KOSA; "
        "Naselje BEŽANIJSKA KOSA PETRA LUBARDE: 9; TOŠIN BUNAR: 42",
    ]))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", source(evidence.source_id))
        item = RawItem(**evidence.model_dump(), external_id="116619",
                       source_html="<article><p>Извещение</p></article>")
        capture_collected_batch(session, [item])
        session.commit()
        profile = load_city_profile("belgrade")
        presentation = build_presentation(session, 1, (evidence,), document_ids=[1],
                                          profile=profile, rubric_id="city_changes")
        draft = present_draft(RussianDraft(title="Отключения света",
                                           body="Проверенный текст.\n\nГде: Белград.\n"
                                                "Когда: 09:00–13:00.\n\n«Дословная цитата»",
                                           claim_ids=("period",)), presentation)
        presentation = bind_native_map_draft(presentation, draft)
        native = presentation.native_map
        html = native["message_html"]
        collapsed = html.index("<blockquote expandable>")
        assert html.index("Проверенный текст.") < collapsed
        assert "<b>Где</b>: Белград.\n<b>Когда</b>: <code>09:00–13:00</code>" in html
        assert collapsed < html.index("VOJVODE ŠUPLJIKCA, 42, 44-48, BB")
        assert "• BRANKA KRSMANOVIĆA, 2 · Zvezdara" in html
        assert "<b>13.11.2026 · 13:00–15:00</b>" in html
        assert html.index("BELAĆEVIĆI, JUNKOVAC") < html.index("<blockquote expandable>")
        assert html.index("BEŽANIJSKA KOSA <u>целиком</u>") < collapsed
        assert "PETRA LUBARDE" not in html
        assert "TOŠIN BUNAR, 42 · Novi Beograd" in html
        assert "<blockquote>«Дословная цитата»</blockquote>" in html
        assert html.count("<a ") == 1 and "openstreetmap.org" not in html
        assert "<tg-map" not in html and "Подробности объявления" not in html
        fake = {**native, "schedule": [{"when": "10:00", "items": ["Выдуманный дом"], "areas": []}]}
        fake["message_html"] = native_message_html(fake, draft)
        tampered = bind_native_map_draft(
            presentation.model_copy(update={"native_map": fake}), draft,
        )
        with pytest.raises(ValueError, match="layout does not match evidence"):
            validate_presentation(tampered, profile, (evidence,), draft, rubric_id="city_changes")
    engine.dispose()


def test_generic_bvk_page_cannot_supply_a_map_for_another_announcement():
    document = news().model_copy(update={"url": "https://www.bvk.rs/planirani-radovi/"})
    assert build_utility_point(document, map_html()) is None
