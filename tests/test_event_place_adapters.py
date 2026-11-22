"""Контракт видимых карточек официального сайта Дома омладине."""

from datetime import UTC, datetime

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.services.event_place_adapters import extract_event_adapter

GALLERY_URL = "https://www.cukarica.rs/vest/712-izlozba-slika-koraci-i-tragovi/"
GALLERY_BODY = (
    "Изложба слика „Кораци и трагови“ биће отворена 18. новембра у 19 часова, "
    "у Галерији „Старт '06“ Културног центра „Чукарица“."
)


def gallery_article(body: str = GALLERY_BODY, published: str = "17.11.2026 11:04"):
    from pathlib import Path

    from relocant_media.collectors.cukarica_news import parse_cukarica_article
    from relocant_media.sources.registry import load_source_file

    page = (
        '<div class="page-full-width"><header><h1>Изложба „Кораци и трагови“</h1>'
        f'<p><i class="bi-calendar"></i>{published}</p></header>'
        f'<!-- Excerpt --><div>{body}</div><div class="page-content">Улаз је слободан.</div></div>'
    )
    item = parse_cukarica_article(
        page, load_source_file(Path("cities/belgrade/sources/cukarica_news.yaml")), GALLERY_URL,
    )
    return EvidenceDocument(**{key: getattr(item, key) for key in (
        "source_id", "url", "title", "body", "published_at",
    )}), page


def test_gallery_uses_exact_room_and_dated_source_without_inventing_exhibition_end():
    article, page = gallery_article()
    facts = _adapt(article, page)
    assert facts and facts["starts_at"] == "2026-11-18T19:00:00"
    assert facts["ends_at"] is None
    assert facts["venue"]["name"] == "Kulturni centar Čukarica · Galerija Start '06"
    assert facts["venue"]["address"] == "Turgenjevljeva 5, Beograd"
    assert facts["time_basis"] == "source_cukarica_gallery"
    assert _adapt(article.model_copy(update={"published_at": None}), page) is None


@pytest.mark.parametrize("body", [
    GALLERY_BODY.replace("Старт '06", "Друга галерија"),
    GALLERY_BODY.replace("у 19 часова", "увече"),
    GALLERY_BODY + " Изложба је отказана.",
    GALLERY_BODY + " Још једна изложба 19. новембра у 20 часова.",
    GALLERY_BODY.replace("18. новембра", "18. маја"),
])
def test_gallery_rejects_unknown_room_missing_time_cancellation_and_ambiguous_dates(body):
    article, page = gallery_article(body)
    assert _adapt(article, page) is None

_DOB = "https://domomladine.org"
_CONCERT_PATH = (
    "/koncerti/svetski-dan-flamenka-u-domu-omladine-beograda-el-sonido-de-la-vida-zvuk-zivota/"
)
_CONCERT = _DOB + _CONCERT_PATH
_INTERVIEW = _DOB + "/karlos-sances-flamenko-gitarista-u-susret-koncertu-u-domu-omladine/"
_NAME = "El Sonido de la Vida – Zvuk života"
_HEADING = "Svetski dan flamenka u Domu omladine Beograda: " + _NAME.upper()
_CONCERT_TIME = "Ponedeljak, 16. novembar 2026. u 20:00"
_LEAD = (
    'Povodom Svetskog dana flamenka, publika će 16. novembra od 20 časova u '
    'Domu omladine Beograda imati priliku da doživi '
    f'<a href="{_CONCERT}">„<span>El Sonido de la Vida</span> – Zvuk života“</a>.'
)


def _source(url: str = _INTERVIEW) -> EvidenceDocument:
    return EvidenceDocument(
        source_id="venue_youth_centre", url=url, title="Koncert u Domu omladine",
        body=f"Publika će 16. novembra od 20 časova u Domu omladine doživeti {_NAME}.",
        published_at=datetime(2026, 11, 12, 10, 48, tzinfo=UTC),
    )


def _page(url: str, article: str, slides: str = "") -> str:
    return (
        f'<html><head><link rel="canonical" href="{url}"></head><body>'
        f'<div id="content">{article}<div class="events-slider-wrapper">{slides}</div></div>'
        '</body></html>'
    )


def _concert(
    date: str = _CONCERT_TIME, room: str = "DOB//Amerikana", name: str = _HEADING,
) -> str:
    return (
        '<article class="type-concert status-publish"><header class="entry-header">'
        f'<h1 class="entry-title"><span class="main-title">{name}</span></h1>'
        f'<p class="entry-datetime"><b>{date}</b> - <i>{room}</i></p>'
        '</header></article>'
    )


def _interview(lead: str = _LEAD) -> str:
    return (
        '<article class="type-post status-publish"><header class="entry-header">'
        '<h1 class="entry-title"><span class="main-title">Intervju</span></h1>'
        '<p class="entry-datetime"><b>12.11.2026.</b></p>'
        f'<div class="entry-excerpt"><p>{lead}</p></div></header></article>'
    )


def _slide(
    url: str = _CONCERT_PATH, name: str = _HEADING,
    date: str = _CONCERT_TIME, room: str = "DOB//Amerikana",
) -> str:
    return (
        '<div class="event-slide">'
        f'<h3 class="entry-title"><a href="{url}"><span class="main-title">{name}</span></a></h3>'
        f'<p class="slide-location">{date}</p><p class="slide-date">{room}</p>'
        '</div>'
    )


def _adapt(document: EvidenceDocument, html: str):
    return extract_event_adapter(document, html, profile=load_city_profile("belgrade"))


def test_direct_concert_uses_explicit_year_and_official_address():
    html = _page(_CONCERT, _concert(), _slide(date="Subota, 14. novembar 2026. u 20:00"))
    facts = _adapt(_source(_CONCERT), html)
    assert facts == {
        "event_name": _HEADING,
        "venue": {
            "name": "Dom omladine Beograda · Amerikana",
            "address": "Makedonska 22/IV, Beograd",
            "latitude": 44.815407,
            "longitude": 20.462793,
        },
        "starts_at": "2026-11-16T20:00:00",
        "ends_at": None,
        "time_basis": "source_dob_concert_header",
        "schedule_source_url": _CONCERT,
        "coordinate_source_url": "https://www.openstreetmap.org/way/41234985",
        "venue_source_url": _DOB + "/kontakt/",
    }


def test_interview_uses_linked_card_and_not_interview_publication():
    html = _page(_INTERVIEW, _interview(), _slide() + _slide(url="/koncerti/drugi/"))
    facts = _adapt(_source(), html)
    assert facts and facts["event_name"] == _NAME
    assert facts["starts_at"] == "2026-11-16T20:00:00"
    assert facts["time_basis"] == "source_dob_linked_concert"
    assert facts["schedule_source_url"] == _CONCERT


@pytest.mark.parametrize("slides", [
    "",
    _slide(url="/koncerti/drugi/"),
    _slide() + _slide(),
    _slide(date="Ponedeljak, 16. novembar u 20:00"),
    _slide(date="Ponedeljak, 16. novembar 2026."),
    _slide(date="Nedelja, 16. novembar 2026. u 20:00"),
    _slide(date="Ponedeljak, 16. novembar 2026. u 21:00"),
    _slide(name="Drugi koncert u istom prostoru"),
    _slide(name="OTKAZAN: " + _HEADING),
    _slide(room="Zidić na Platou Milana Mladenovića"),
])
def test_unlinked_ambiguous_rescheduled_or_unverified_venue_is_ignored(slides):
    assert _adapt(_source(), _page(_INTERVIEW, _interview(), slides)) is None


def test_mismatched_source_or_lead_cannot_borrow_event_coordinates():
    doc = _source()
    html = _page(_INTERVIEW, _interview(), _slide())
    assert _adapt(doc.model_copy(update={"source_id": "elsewhere"}), html) is None
    assert _adapt(doc.model_copy(update={"body": "Drugi događaj."}), html) is None
    assert _adapt(doc, html.replace(_INTERVIEW, _INTERVIEW + "other")) is None
    other_venue = html.replace("u Domu omladine Beograda imati", "u drugoj hali imati")
    assert _adapt(doc, other_venue) is None


STAGE_BODY = (
    "Кабаре ће бити изведено на великој сцени Културног центра „Чукарица“ "
    "24. новембра, са почетком у 20 часова."
)


def test_main_stage_takes_year_from_publication_like_gallery():
    # Общее правило площадки вместо отдельной ветки под один анонс (#396).
    article, page = gallery_article(STAGE_BODY, published="13.11.2026 14:45")
    facts = _adapt(article, page)
    assert facts and facts["starts_at"] == "2026-11-24T20:00:00"
    assert facts["venue"]["name"] == "Kulturni centar Čukarica · Velika scena"
    assert facts["schedule_source_url"] == article.url


def test_main_stage_rejects_event_far_from_publication():
    article, page = gallery_article(STAGE_BODY.replace("новембра", "маја"),
                                    published="13.11.2026 14:45")
    assert _adapt(article, page) is None


def test_main_stage_december_announcement_for_january_without_year():
    article, page = gallery_article(STAGE_BODY.replace("24. новембра", "10. јануара"),
                                    published="20.12.2026 10:00")
    facts = _adapt(article, page)
    assert facts and facts["starts_at"] == "2027-01-10T20:00:00"


def test_explicit_past_year_is_not_moved_forward():
    article, page = gallery_article(
        STAGE_BODY.replace("24. новембра,", "10. јануара 2026. године,"),
        published="20.12.2026 10:00")
    assert _adapt(article, page) is None
