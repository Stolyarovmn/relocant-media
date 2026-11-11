import copy
import json

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services.exhibition_map_point import (
    build_exhibition_map_review,
    validate_exhibition_map_review,
)


def fixture(city="Belgrade", country="RS", latitude="44.8101", longitude="20.4602"):
    # Синтетические координаты проверяют переносимость механизма, не реальные места.
    url = "https://museum.example/exhibition"
    event = {
        "@context": "https://schema.org",
        "@type": "ExhibitionEvent",
        "url": url,
        "name": "Выставка графики",
        "location": {
            "@type": "Place", "name": "Галерея <А> & Б",
            "address": {"addressLocality": city, "addressCountry": country},
            "geo": {"@type": "GeoCoordinates", "latitude": latitude, "longitude": longitude},
        },
    }
    document = EvidenceDocument(
        source_id="museum", url=url, title=event["name"],
        body=f'Место выставки: {event["location"]["name"]}. {city}, {country}.',
    )
    parameters = {
        "primary_source_url": url, "exhibition_name": event["name"],
        "venue_name": event["location"]["name"], "city": city, "country": country,
    }
    return document, event, parameters


def html(data):
    return (
        '<script type="application/ld+json">'
        + json.dumps(data, ensure_ascii=False) + '</script>'
    )


@pytest.mark.parametrize("city,country,latitude,longitude", [
    ("Belgrade", "RS", "44.8101", "20.4602"),
    ("Lisbon", "PT", "38.7141", "-9.1422"),
])
def test_point_review_is_reproducible_portable_and_not_a_delivery(
    city, country, latitude, longitude,
):
    document, event, parameters = fixture(city, country, latitude, longitude)
    source = html(event)
    packet = build_exhibition_map_review(document, source, **parameters)
    validate_exhibition_map_review(json.loads(json.dumps(packet)), document, source)
    assert packet["mode"] == "point_map_review"
    assert packet["rich_block"]["location"] == {
        "latitude": float(latitude), "longitude": float(longitude),
    }
    assert '<tg-map lat="' + latitude in packet["rich_html"]
    assert "Галерея &lt;А&gt; &amp; Б" in packet["rich_html"]
    assert "<А>" not in packet["rich_html"]
    assert packet["publication_permitted"] is False
    assert packet["requires_independent_reviews"] == ["facts", "utility"]
    assert packet["network_access_performed"] is False
    assert packet["geocoding_performed"] is False
    assert "chat_id" not in packet and "sendWidget" not in str(packet)


@pytest.mark.parametrize("latitude,longitude", [
    (None, "20"), ("91", "20"), ("44", "181"), ("NaN", "20"),
    (True, "20"), ("44.5N", "20"), ("44,5", "20"), ("44", "Infinity"),
    ('44\"/><script>bad</script>', "20"),
])
def test_invalid_or_missing_coordinates_do_not_produce_map(latitude, longitude):
    document, event, parameters = fixture(latitude=latitude, longitude=longitude)
    packet = build_exhibition_map_review(document, html(event), **parameters)
    assert packet["mode"] == "text"
    assert "rich_html" not in packet


def test_addresses_organization_geo_viewport_and_other_event_are_not_exhibition_points():
    document, event, parameters = fixture()
    cases = []
    address_only = copy.deepcopy(event)
    address_only["location"].pop("geo")
    cases.append(html(address_only))
    organization = copy.deepcopy(event)
    organization["@type"] = "Organization"
    organization["geo"] = organization["location"]["geo"]
    cases.append(html(organization))
    other_event = copy.deepcopy(event)
    other_event["url"] = "https://museum.example/other-exhibition"
    cases.append(html(other_event))
    cases.append('<article><a href="https://maps.google.com/maps/@44.81,20.46,15z">Карта</a></article>')
    for source in cases:
        assert build_exhibition_map_review(document, source, **parameters)["mode"] == "text"


def test_city_venue_ambiguous_location_and_unsafe_source_fail_closed():
    document, event, parameters = fixture()
    for changes in ({"city": "Lisbon"}, {"venue_name": "Другая галерея"},
                    {"primary_source_url": "https://127.0.0.1/exhibition"}):
        assert build_exhibition_map_review(document, html(event), **{
            **parameters, **changes,
        })["mode"] == "text"
    graph = {"@context": "https://schema.org", "@graph": [event, copy.deepcopy(event)]}
    graph["@graph"][1]["location"]["geo"]["latitude"] = "44.1"
    assert build_exhibition_map_review(document, html(graph), **parameters)["mode"] == "text"
    event["location"] = [event["location"], copy.deepcopy(event["location"])]
    assert build_exhibition_map_review(document, html(event), **parameters)["mode"] == "text"


def test_any_change_to_source_body_coordinate_or_caption_invalidates_review():
    document, event, parameters = fixture()
    source = html(event)
    packet = build_exhibition_map_review(document, source, **parameters)
    for changed in (
        {**packet, "rich_html": packet["rich_html"].replace('lat="44.8101"', 'lat="43"')},
        {**packet, "caption": "Подменённая подпись"},
        {**packet, "publication_permitted": True},
    ):
        with pytest.raises(ValueError, match="exact source"):
            validate_exhibition_map_review(changed, document, source)
    with pytest.raises(ValueError, match="exact source"):
        validate_exhibition_map_review(packet, document, source + " ")
    with pytest.raises(ValueError, match="exact source"):
        validate_exhibition_map_review(
            packet, document.model_copy(update={"body": document.body + " Изменение"}), source,
        )


@pytest.mark.parametrize("zoom", [-1, 25, True, 1.5])
def test_invalid_zoom_is_rejected(zoom):
    document, event, parameters = fixture()
    with pytest.raises(ValueError, match="zoom"):
        build_exhibition_map_review(document, html(event), **parameters, zoom=zoom)


def test_duplicate_json_ld_coordinate_fields_are_not_resolved_by_guessing():
    document, event, parameters = fixture()
    source = html(event).replace('"latitude": "44.8101"',
                                '"latitude": "44.8101", "latitude": "45"')
    assert build_exhibition_map_review(document, source, **parameters)["mode"] == "text"
