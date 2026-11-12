"""Минимальные официальные формы a116609/BVK и отрицательные примеры без сети."""

from typing import Any

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services import water_outage_scope as scope

CITY_URL = ("https://www.beograd.rs/lat/servisne-informacije/vodovod-i-kanalizacija/"
            "a116609/Planirani-radovi-na-vodovodnoj-mrezi.html")
CITY_BODY = (
    "Zbog planiranih radova na vodovodnoj mreži u opštini Voždovac, u petak, "
    "13. novembra 2026. godine, u periodu od 8.30 do 18.00 sati, bez vode će ostati "
    "potrošači u Ulici kralja Bodina, na delu od Vidske do Kruševačke ulice. "
    "Za najnužnije potrebe za pijaćom vodom obezbeđena je auto-cisterna."
)
NEWS_URL = ("https://www.bvk.rs/prekidi-u-snabdevanju-vodom-i-umanjen-pritisak-u-naselju-"
            "ledine-i-delu-naselja-surcin/")
NEWS_TITLE = "Прекиди у снабдевању водом и умањен притисак у насељу Ледине и делу насеља Сурчин"
NEWS_BODY = (
    "У протеклом периоду ЈКП „Београдски водовод и канализација“ бележи повећан број "
    "пријава грађана који се односе на повремене прекиде у снабдевању водом у насељу "
    "Ледине и делу насеља Сурчин."
)
MAP_URL = "https://www.bvk.rs/wp-content/uploads/2026/06/ledine-mapa-1030x601.webp"


def document(**changes: Any) -> EvidenceDocument:
    return EvidenceDocument.model_validate({
        "source_id": "city_beograd", "url": CITY_URL,
        "title": "Плановые работы 13.11.2026", "body": CITY_BODY, **changes,
    })


def news(**changes: Any) -> EvidenceDocument:
    return document(source_id="bvk_outage_news", url=NEWS_URL,
                    title=NEWS_TITLE, body=NEWS_BODY, **changes)


def map_html(*, href: str = MAP_URL, title: str = "ledine-mapa") -> str:
    # Проверенная структура article post-91008; thumbnail — wp-image-91012.
    return (
        f'<link rel="canonical" href="{NEWS_URL}"><article class="post-entry post-91008">'
        '<div class="blog-meta"><a class="small-preview" '
        f'href="{href}" itemprop="image" itemtype="https://schema.org/ImageObject" '
        f'title="{title}"><img class="wp-image-91012 wp-post-image" '
        'src="https://www.bvk.rs/wp-content/uploads/2026/06/ledine-mapa-180x180.webp">'
        f'</a></div><blockquote class="first-quote">{NEWS_TITLE}</blockquote>'
        f'<div class="entry-content" itemprop="text"><p>{NEWS_BODY}</p></div></article>'
    )


def test_real_city_segment_preserves_source_names_and_case() -> None:
    assert scope.extract_water_segments(document()) == [{
        "municipality": "Voždovac", "street": "kralja Bodina",
        "start": "Vidske", "end": "Kruševačke",
    }]


def test_only_explicit_full_settlement_is_eligible() -> None:
    text = CITY_BODY.split("potrošači", 1)[0] + "potrošači u celom naselju Ripanj."
    assert scope.extract_water_settlement(document(body=text)) == ("Voždovac", "Ripanj")
    assert scope.extract_water_settlement(document(body=text.replace("celom ", ""))) is None
    assert scope.extract_water_settlement(document(body=text + " Osim Ulice Broj Jedan.")) is None


def test_real_cyrillic_bvk_segment() -> None:
    body = (
        "Због планираних радова на водоводној мрежи у општини Вождовац, у петак, "
        "13. новембра 2026. године, у периоду од 8.30 до 18.00 сати, без воде ће остати "
        "потрошачи у Улици краља Бодина, на делу од Видске до Крушевачке улице."
    )
    assert scope.extract_water_segments(document(
        source_id="bvk_planned_works", url="https://www.bvk.rs/planirani-radovi/", body=body,
    )) == [{"municipality": "Вождовац", "street": "краља Бодина",
            "start": "Видске", "end": "Крушевачке"}]


def test_second_word_order_and_dated_bvk_title() -> None:
    body = ("Због радова на општини Врачар, без воде ће остати потрошачи "
            "у Хаџи Мелентијевој улици од Браничевске до Петра Кочића.")
    assert scope.extract_water_segments(document(
        source_id="bvk_planned_works", url="https://bvk.rs/planirani-radovi/",
        title="29.09.2026. Планирани радови у општини Врачар", body=body,
    )) == [{"municipality": "Врачар", "street": "Хаџи Мелентијевој",
            "start": "Браничевске", "end": "Петра Кочића"}]


@pytest.mark.parametrize("body", [
    CITY_BODY.replace("13. novembra 2026.", "31. novembra 2026."),
    CITY_BODY.replace("13. novembra 2026.", "sutra"),
    CITY_BODY.replace("u opštini Voždovac,", "u Beogradu,"),
    CITY_BODY.replace("bez vode će ostati potrošači", "izvode se radovi"),
    CITY_BODY.replace("do Kruševačke ulice", "do kućnog broja 12"),
    CITY_BODY.replace("do Kruševačke ulice", "do okolnih ulica"),
    CITY_BODY.replace("do Kruševačke ulice", "do Vidske ulice"),
    CITY_BODY.replace("na delu od Vidske do Kruševačke ulice", "i okolnim ulicama"),
    CITY_BODY + " U opštini Vračar, 14. novembra 2026. godine takođe su radovi.",
    CITY_BODY + " U opštini Vračar, privremeno se izvode radovi.",
])
def test_ambiguous_or_incomplete_scope_is_not_invented(body: str) -> None:
    assert scope.extract_water_segments(document(body=body)) == []


@pytest.mark.parametrize("changes", [
    {"source_id": "rss_repost"},
    {"url": CITY_URL.replace("https:", "http:")},
    {"url": CITY_URL.replace("www.beograd.rs", "www.beograd.rs.evil.test")},
    {"url": CITY_URL.replace("www.beograd.rs", "evil@www.beograd.rs")},
    {"url": CITY_URL.replace("vodovod-i-kanalizacija", "saobracaj-i-prevoz")},
    {"url": CITY_URL + "?"},
    {"url": CITY_URL + "#map"},
])
def test_source_provenance_required(changes: dict[str, Any]) -> None:
    assert scope.extract_water_segments(document(**changes)) == []


def test_real_bvk_map_href_only_no_download_or_original_url_invention() -> None:
    assert scope.extract_official_water_map(news(), map_html()) == MAP_URL


def test_explicit_map_link_in_matching_article_body() -> None:
    html = map_html().replace('class="blog-meta"', 'class="entry-content"')
    html = html.replace(' itemprop="image" itemtype="https://schema.org/ImageObject"', "")
    assert scope.extract_official_water_map(news(), html) == MAP_URL


@pytest.mark.parametrize("href", [
    MAP_URL.replace("https:", "http:"),
    MAP_URL.replace("www.bvk.rs", "www.bvk.rs.evil.test"),
    MAP_URL.replace("www.bvk.rs", "attacker@www.bvk.rs"),
    MAP_URL.replace("www.bvk.rs", "127.0.0.1"),
    MAP_URL + "?token=x", MAP_URL + "#map", MAP_URL.replace(".webp", ".svg"),
    "https://www.bvk.rs/wp-content/uploads/2020/01/zatvarac-662.jpg",
    "https://www.beogradsad.rs/lat/mape",
    "https://www.bvk.rs/kvarovi-na-mrezi/#cisterne",
])
def test_unsafe_or_unrelated_assets_rejected(href: str) -> None:
    assert scope.extract_official_water_map(news(), map_html(href=href)) is None


@pytest.mark.parametrize("transform", [
    lambda html: html.replace('class="blog-meta"', 'class="banner"'),
    lambda html: html.replace('class="blog-meta"', 'class="blog-meta" hidden'),
    lambda html: html.replace('<article ', '<article hidden '),
    lambda html: '<nav>' + html + '</nav>',
    lambda html: html.replace('<div class="blog-meta">', '<aside class="blog-meta">'),
    lambda html: html.replace('itemtype="https://schema.org/ImageObject"', 'itemtype="unknown"'),
    lambda html: html.replace(NEWS_TITLE, "Другое объявление"),
    lambda html: html.replace(NEWS_BODY, "Другой текст объявления"),
    lambda html: html.replace('rel="canonical"', 'rel="alternate"'),
    lambda html: html.replace(NEWS_URL, "https://www.bvk.rs/another-news/"),
    lambda html: html + html,
])
def test_map_must_belong_to_exact_visible_article(transform: Any) -> None:
    assert scope.extract_official_water_map(news(), transform(map_html())) is None


def test_two_maps_are_ambiguous() -> None:
    extra = ('<a title="other-mapa" '
             'href="https://www.bvk.rs/wp-content/uploads/2026/06/other-mapa.webp">Карта</a>')
    html = map_html().replace(f"<p>{NEWS_BODY}</p>", f"<p>{NEWS_BODY}</p>{extra}")
    assert scope.extract_official_water_map(news(), html) is None


def test_cistern_map_is_not_an_outage_map() -> None:
    html = map_html(href=MAP_URL.replace("ledine-mapa", "cisterne-mapa"), title="cisterne-mapa")
    assert scope.extract_official_water_map(news(), html) is None


def test_water_news_identity_required_and_city_banner_is_not_map() -> None:
    assert scope.extract_official_water_map(document(), map_html()) is None
    wrong = news().model_copy(update={"body": "Другая новость без сведений о водоснабжении." * 3})
    assert scope.extract_official_water_map(wrong, map_html()) is None
    assert scope.extract_official_water_map(news(), map_html(title="Фото")) is None


def test_size_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scope, "MAX_DOCUMENT_BYTES", 10)
    assert scope.extract_water_segments(document()) == []
    assert scope.extract_official_water_map(news(), map_html()) is None
    monkeypatch.setattr(scope, "MAX_DOCUMENT_BYTES", 100_000)
    monkeypatch.setattr(scope, "MAX_HTML_BYTES", 10)
    assert scope.extract_official_water_map(news(), map_html()) is None
