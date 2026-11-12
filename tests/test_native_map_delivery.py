import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import escape
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_editorial_package import bind_context, parse
from test_independent_reviews import Provider

from relocant_media.ai.models import EvidenceDocument, RussianDraft, SelectionBrief
from relocant_media.ai.package import canonical_json, context_sha256, draft_sha256, evaluate_package
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import event_key
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.ingestion.media_provenance import build_media_provenance
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import DeliveryBlockedError, _candidate, _snapshot_member
from relocant_media.services.editorial_products import (
    build_presentation,
    present_draft,
    validate_presentation,
    verify_live_map,
    verify_stored_presentation,
)
from relocant_media.services.native_map import bind_native_map_draft
from relocant_media.services.owner_review import OWNER_REVIEW_KIND, deliver_owner_approved
from relocant_media.services.package_import import import_package
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, EditorialRun, RawItem, Source, Submission


@pytest.fixture
def session(monkeypatch, tmp_path):
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    monkeypatch.setenv("EDITORIAL_MEDIA_RELEASE_DIR", str(tmp_path / "no-media"))
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def payload(session, *, city="belgrade", map_enabled=True, country_value=None, timed=False):
    profile = load_city_profile("belgrade")
    locality, country, latitude, longitude = "Belgrade", "RS", "44.8101", "20.4602"
    if city == "lisbon":
        locality, country, latitude, longitude = "Lisbon", "PT", "38.7141", "-9.1422"
        profile = profile.model_copy(update={
            "city_id": city, "city_name": "Лиссабон", "country_name": "Португалия",
            "country_code": country, "timezone": "Europe/Lisbon",
            "geography": profile.geography.model_copy(update={
                "city_aliases": (locality,), "country_aliases": (country,),
            }),
        })
    url = f"https://museum.{city}.example/exhibition"
    title, venue = "Выставка графики", "Галерея А"
    date = (datetime.now(UTC) + timedelta(days=2)).strftime("%d.%m.%Y")
    body = f"{title} пройдёт в {venue}, {profile.city_name}, {date} в 12:00. Вход свободный."
    source = Source(id="gallery", city=city, name="Галерея", url=url,
                    source_type="official_venue_events", trust=Decimal("1.000"),
                    ownership="official", auto_publish=True, enabled=True, high_risk=False)
    session.add(source)
    session.flush()
    published = datetime.now(UTC) - timedelta(hours=1)
    raw = RawItem(source_id=source.id, external_id="exhibition", url=url, title=title,
                  body=body, published_at=published)
    session.add(raw)
    session.flush()
    event = {"@context": "https://schema.org", "@type": "ExhibitionEvent", "url": url,
             "name": title, "location": {"@type": "Place", "name": venue,
             "address": {"addressLocality": locality,
                         "addressCountry": country if country_value is None else country_value},
             "geo": {"@type": "GeoCoordinates", "latitude": latitude, "longitude": longitude}}}
    if timed:
        event["startDate"] = datetime.strptime(date, "%d.%m.%Y").replace(hour=12).isoformat()
        event["location"]["address"]["streetAddress"] = "Knez Mihailova 1"
    html = f"<article><p>{escape(body)}</p></article>"
    if map_enabled:
        html += '<script type="application/ld+json">' + json.dumps(event) + '</script>'
    saved_html, manifest = build_media_provenance(CollectedItem(
        source_id=source.id, external_id=raw.external_id, url=url, title=title, body=body,
        published_at=None, source_html=html,
    ))
    session.add(RawItemMediaSnapshot(
        raw_item_id=raw.id, source_page=url, source_html=saved_html,
        source_html_sha256=manifest["source_html_sha256"],
        source_html_bytes=manifest["source_html_bytes"], manifest=manifest,
    ))
    session.commit()
    document = EvidenceDocument(source_id=source.id, url=url, title=title, body=body,
                                published_at=published)
    presentation = build_presentation(session, raw.id, (document,), document_ids=[raw.id],
                                      profile=profile, rubric_id="weekend", source_url=url)
    draft = present_draft(RussianDraft(title=title, body=body, claim_ids=("c1",)), presentation)
    presentation = bind_native_map_draft(presentation, draft)
    selection = SelectionBrief(
        policy_version=profile.policy_version, profile_sha256=profile.sha256,
        audience="Жители города, интересующиеся искусством",
        benefit="Выбрать бесплатную выставку и спланировать посещение",
        novelty="Музей объявил новое открытие выставки графики",
        why_now="До открытия остаётся два дня для планирования визита",
        rubric_id="weekend", locality="city", value_kind="experience",
        evidence_refs=({"source_id": source.id, "url": url, "quote": body},),
    )
    data = {
        "version": 2, "policy_version": "subscription-editorial-v2", "city": city,
        "raw_item_id": raw.id, "source_id": source.id, "article_id": raw.external_id,
        "evidence": document.model_dump(mode="json"),
        "event": {"event_key": event_key(_candidate(raw)),
                  "editorial_rule_version": "belgrade-editorial-v2",
                  "documents": [_snapshot_member(raw, source)]},
        "presentation": presentation.model_dump(mode="json"),
        "outcome": {"status": "ready", "reason": "reviewed", "confidence": 0.99,
                    "ranking": {"should_process": True, "relevance": 0.99, "category": "events",
                                "confidence": 0.99, "rationale": "Полезное городское событие",
                                "selection": selection.model_dump(mode="json")},
                    "claims": {"claims": [{"claim_id": "c1", "text": body, "material": True,
                                "citations": [{"source_id": source.id, "url": url,
                                               "quote": body}]}]},
                    "verification": {"claims": [{"claim_id": "c1", "supported": True,
                                    "confidence": 0.99, "reason": "Подтверждено первоисточником"}]},
                    "draft": draft.model_dump(mode="json")},
        "reviews": [{"reviewer_id": f"test-{role}", "role": role, "source_id": source.id,
                     "article_id": raw.external_id, "draft_sha256": draft_sha256(draft),
                     "context_sha256": "0" * 64, "reviewed_at": datetime.now(UTC).isoformat(),
                     "verdict": "approve", "score": 95, "flags": [], "live_source_checked": True}
                    for role in ("facts", "utility")],
    }
    bind_context(data)
    return data, profile, document, html


@pytest.mark.parametrize("city", ["belgrade", "lisbon"])
def test_snapshot_to_exact_html_is_portable_and_bound_before_reviews(session, city):
    data, profile, document, html = payload(session, city=city)
    package = parse(data)
    presentation = package.presentation
    assert presentation.native_map is not None
    assert "<tg-map " in presentation.native_map["message_html"]
    validate_presentation(presentation, profile, (document,), package.outcome.draft,
                          rubric_id="weekend")
    verify_stored_presentation(session, package.raw_item_id, (document,),
                              document_ids=[package.raw_item_id], presentation=presentation)
    verify_live_map(presentation, document, html + "<footer>Обновление</footer>")
    with pytest.raises(ValueError, match="live source"):
        verify_live_map(presentation, document, html.replace('"latitude": "', '"latitude": "1'))
    assert presentation.illustration["illustration_mode"] == "text"


@pytest.mark.parametrize("country_value", [{"@type": "Country", "name": "RS"}, ["RS"]])
def test_optional_unsupported_json_ld_country_falls_back_to_text(session, country_value):
    data, _, _, _ = payload(session, country_value=country_value)
    assert "native_map" not in data["presentation"]


def test_legacy_presentation_omits_none_without_changing_hash(session):
    data, _, _, _ = payload(session, map_enabled=False)
    original = copy.deepcopy(data["presentation"])
    assert "native_map" not in original
    assert "event_place" not in original
    value = EditorialPresentation.model_validate(original)
    assert value.native_map is None
    assert value.model_dump(mode="json") == original
    assert hashlib.sha256(canonical_json(original).encode()).hexdigest() == hashlib.sha256(
        canonical_json(value.model_dump(mode="json")).encode()).hexdigest()


def test_event_presentation_binds_afisha_to_saved_and_live_source(session):
    data, profile, document, html = payload(session, timed=True)
    package = parse(data)
    presentation = package.presentation
    assert presentation.native_map is None
    assert presentation.illustration["preview_url"] == document.url
    assert presentation.event_place["venue"]["name"] == "Галерея А"
    assert presentation.event_place["ends_at"] is None
    docs = tuple(package.event.documents)
    validate_presentation(presentation, profile, docs, package.outcome.draft, rubric_id="weekend")
    verify_stored_presentation(session, package.raw_item_id, docs,
                              document_ids=[package.raw_item_id], presentation=presentation)
    verify_live_map(presentation, document, html + "<footer>Обновление</footer>")
    with pytest.raises(ValueError, match="расписание изменились"):
        verify_live_map(presentation, document, html.replace("T12:00:00", "T13:00:00"))


def test_source_coordinate_and_full_html_tampering_block_import(session):
    data, _, _, _ = payload(session)
    data["presentation"]["native_map"]["message_html"] += "<script>bad()</script>"
    bind_context(data)  # Даже фиктивно обновлённые подписи не разрешают посторонний HTML.
    with pytest.raises(ValueError, match="HTML changed"):
        import_package(session, parse(data))
    assert session.get(EditorialRun, data["raw_item_id"]) is None


@pytest.mark.parametrize("native", [{}, {"point": []}, {"point": {}}, {"point": "bad"}])
def test_malformed_reviewed_native_map_is_controlled_error_before_claim(session, native):
    data, _, _, _ = payload(session)
    data["presentation"]["native_map"] = native
    bind_context(data)
    with pytest.raises(ValueError, match="native map"):
        import_package(session, parse(data))
    assert session.get(EditorialRun, data["raw_item_id"]) is None


def test_map_requires_two_reviews_bound_to_exact_markup(session):
    data, _, _, _ = payload(session)
    data["reviews"][1]["reviewer_id"] = data["reviews"][0]["reviewer_id"]
    with pytest.raises(ValueError, match="distinct"):
        evaluate_package(parse(data))
    data["reviews"][1]["reviewer_id"] = "different-reviewer"
    data["reviews"][1]["context_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="review context"):
        evaluate_package(parse(data))


@pytest.mark.asyncio
async def test_full_reviewed_package_uses_rich_api_and_durable_single_receipt(session):
    data, _, _, _ = payload(session)
    run = import_package(session, parse(data))
    assert run.outcome["status"] == "ready"
    requests = []

    def handler(request):
        assert session.get(EditorialRun, run.raw_item_id).delivery_status == "sending"
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 313}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = SimpleNamespace(bot=TelegramBotAPI("fake-token", client))
        first = await deliver_reviewed(session, run.raw_item_id, publisher,
                                       target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
        second = await deliver_reviewed(session, run.raw_item_id, publisher,
                                        target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
    assert first.message_id == second.message_id == 313
    assert len(requests) == 1 and requests[0].url.path.endswith("/sendRichMessage")
    sent = json.loads(requests[0].content)
    expected = data["presentation"]["native_map"]
    assert sent == {"chat_id": APPROVED_CHANNEL_ID,
                    "rich_message": {"html": expected["message_html"]}}
    assert run.outcome["_rich_delivery"] == {
        "state": "sent", "message_html_sha256": expected["message_html_sha256"],
        "planned_message_count": 1, "message_ids": [313],
    }


@pytest.mark.asyncio
async def test_afisha_button_uses_the_reviewed_event_id_after_the_delivery_claim(session):
    data, _, document, _ = payload(session, timed=True)
    run = import_package(session, parse(data))
    requests = []
    place = data["presentation"]["event_place"]

    def handler(request):
        assert session.get(EditorialRun, run.raw_item_id).delivery_status == "sending"
        assert request.url.path.endswith("/sendMessage")
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 314}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = SimpleNamespace(bot=TelegramBotAPI("fake-token", client))
        result = await deliver_reviewed(session, run.raw_item_id, publisher,
                                        target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
    assert result.message_id == 314 and len(requests) == 1
    sent = requests[0]
    assert sent["link_preview_options"]["url"] == document.url
    button = sent["reply_markup"]["inline_keyboard"][0][0]
    assert button["url"].endswith(f"?mode=places&event={place['id']}")
    assert run.outcome["_rich_delivery"]["message_ids"] == [314]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "bad_receipt", "http_error"])
async def test_rich_uncertain_result_never_retries_or_falls_back(session, failure):
    data, _, _, _ = payload(session)
    run = import_package(session, parse(data))
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown remote result", request=request)
        if failure == "http_error":
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": True}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = SimpleNamespace(bot=TelegramBotAPI("fake-token", client))
        with pytest.raises((httpx.ReadTimeout, httpx.HTTPStatusError, TelegramAPIError)):
            await deliver_reviewed(session, run.raw_item_id, publisher,
                                   target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
        assert run.delivery_status == "uncertain"
        assert run.outcome["_rich_delivery"]["state"] == "uncertain"
        with pytest.raises(DeliveryBlockedError, match="manual reconciliation"):
            await deliver_reviewed(session, run.raw_item_id, publisher,
                                   target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_snapshot_tamper_blocks_before_any_send(session):
    data, _, _, _ = payload(session)
    run = import_package(session, parse(data))
    snapshot = session.get(RawItemMediaSnapshot, run.raw_item_id)
    snapshot.source_html += " changed"
    session.commit()
    with pytest.raises(ValueError, match="snapshot"):
        await deliver_reviewed(session, run.raw_item_id, SimpleNamespace(),
                               target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
    assert run.delivery_status == "pending"


@pytest.mark.asyncio
async def test_no_explicit_map_keeps_existing_send_message_path(session):
    data, _, _, _ = payload(session, map_enabled=False)
    run = import_package(session, parse(data))
    calls = []

    class LegacyPublisher:
        async def publish(self, text):
            calls.append(text)
            return 12

    result = await deliver_reviewed(session, run.raw_item_id, LegacyPublisher(),
                                    target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
    assert result.message_id == 12 and len(calls) == 1
    assert "_rich_delivery" not in run.outcome


def test_repair_rebinds_exact_html_before_both_new_reviews(session):
    data, _, document, _ = payload(session)
    for review in data["reviews"]:
        review["verdict"] = "hold"
    original = parse(data)
    provider = Provider()
    first = copy.deepcopy(provider.responses)
    first[0].update(verdict="hold", flags=["Уточнить"], reason="Уточнить заголовок")
    repaired = {**data["outcome"]["draft"], "title": "Открывается бесплатная выставка графики"}
    provider.responses = [*first, repaired, *provider.responses]
    result = review_with_repairs(original, provider, lambda: (document,))
    assert len(provider.calls) == 5
    native = result.presentation.native_map
    assert repaired["title"] in native["message_html"]
    assert native["message_html_sha256"] != original.presentation.native_map["message_html_sha256"]
    assert all(review.context_sha256 == context_sha256(result) for review in result.reviews)
    assert all(review.verdict == "approve" for review in result.reviews)
    for call in provider.calls[-2:]:
        reviewed_native = json.loads(call["prompt"])["package"]["presentation"]["native_map"]
        assert reviewed_native == native


def test_draft_html_is_escaped_and_not_interpreted_as_markup(session):
    data, _, _, _ = payload(session)
    package = parse(data)
    draft = package.outcome.draft.model_copy(update={
        "body": '<script>bad()</script> & <a href="https://evil.example">подмена</a>',
    })
    bound = bind_native_map_draft(package.presentation, draft)
    html = bound.native_map["message_html"]
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert '&lt;a href=&quot;https://evil.example&quot;&gt;' in html
    assert html.count("<tg-map ") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send", "snapshot_changed", "timeout"])
async def test_owner_approved_path_uses_same_verified_rich_transport(session, action):
    data, _, document, html = payload(session)
    source = session.get(Source, data["source_id"])
    source.auto_publish = False
    data["event"]["documents"][0]["auto_publish"] = False
    session.commit()
    bind_context(data)
    package = parse(data)
    run = import_package(session, package)
    assert run.outcome["status"] == "human_review"
    review = Submission(kind=OWNER_REVIEW_KIND, status="approved", telegram_user_id=9001,
                        telegram_message_id=501, payload={
                            "raw_item_id": run.raw_item_id,
                            "package_sha256": run.outcome["_package_sha256"],
                            "draft_sha256": draft_sha256(package.outcome.draft),
                        })
    session.add(review)
    session.commit()
    # Штатный owner callback получает live документы через server_cycle.fetch_documents,
    # который также вызывает verify_live_map до deliver_owner_approved.
    verify_live_map(package.presentation, document, html)
    requests = []

    def handler(request):
        assert session.get(EditorialRun, run.raw_item_id).delivery_status == "sending"
        requests.append(request)
        if action == "timeout":
            raise httpx.ReadTimeout("unknown result", request=request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 211}})

    if action == "snapshot_changed":
        session.get(RawItemMediaSnapshot, run.raw_item_id).source_html += "changed"
        session.commit()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = SimpleNamespace(bot=TelegramBotAPI("fake-token", client))
        args = {"live_documents": (document,), "target_channel_id": APPROVED_CHANNEL_ID,
                "enabled": True}
        if action == "snapshot_changed":
            with pytest.raises(ValueError, match="snapshot"):
                await deliver_owner_approved(session, review, publisher, **args)
            assert requests == [] and run.delivery_status == "pending"
        elif action == "timeout":
            with pytest.raises(httpx.ReadTimeout):
                await deliver_owner_approved(session, review, publisher, **args)
            assert run.delivery_status == "uncertain"
            with pytest.raises(DeliveryBlockedError, match="pending reviewed"):
                await deliver_owner_approved(session, review, publisher, **args)
            assert len(requests) == 1
        else:
            sent = await deliver_owner_approved(session, review, publisher, **args)
            assert sent.message_id == 211 and run.delivery_status == "sent"
            assert requests[0].url.path.endswith("/sendRichMessage")
            assert json.loads(requests[0].content)["rich_message"]["html"] == (
                data["presentation"]["native_map"]["message_html"])
            assert run.outcome["_rich_delivery"]["message_ids"] == [211]
