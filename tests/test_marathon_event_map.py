from dataclasses import replace
from datetime import UTC, datetime

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.belgrade_marathon import parse_marathon_program
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import EventCandidate, same_event
from relocant_media.domain.event_programs import MARATHON_PAGE_URL
from relocant_media.services.event_place import resolve_event_place
from relocant_media.services.marathon_event_map import attach_marathon_kml

_BODY = """Datum održavanja: nedelja, 22. novembar 2026.
Vreme događaja: 08:00–14:00
Start trke: 10:00, u talasima
Discipline: 10K i 21K (polumaraton)
Organizator: Beogradski maraton"""
_HTML = f"""<html><head><link rel="canonical" href="{MARATHON_PAGE_URL}"/></head>
<body><h1>7. Sava osiguranje Beogradski polumaraton</h1><p>{_BODY}</p>
<iframe src="https://www.google.com/maps/d/embed?ehbc=2E312F&amp;mid=1wLdBTheHVqeu9iZ88dKCzF9QcnQ">
</iframe></body></html>"""
# Сокращённая тестовая структура официального экспорта с отдельным архивом.
_KML = """<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Folder><name>Sadržaji</name><Placemark><name>Start</name>
<Point><coordinates>20.421414,44.8162991,0</coordinates></Point></Placemark></Folder>
<Folder><name>Sava osiguranje Beogradski polumaraton - 10K</name>
<Placemark><name>10K</name><LineString>
<coordinates>20.421411,44.816289,0 20.422724,44.815353,0</coordinates>
</LineString></Placemark></Folder>
<Folder><name>Sava osiguranje Beogradski polumaraton - 21K</name>
<Placemark><name>POLUMARATON</name><LineString>
<coordinates>20.42141,44.816288,0 20.4059,44.8228,0 20.422692,44.815324,0</coordinates>
</LineString></Placemark></Folder>
<Folder><name>Stara trasa</name><Placemark><name>10K HM2025</name>
<LineString><coordinates>20.421412,44.816286,0 20.422562,44.815435,0</coordinates>
</LineString></Placemark></Folder></Document></kml>"""


def _document(**changes):
    fields = {
        "source_id": "venue_belgrade_marathon",
        "url": MARATHON_PAGE_URL,
        "title": "7. Sava osiguranje Beogradski polumaraton",
        "body": _BODY,
    }
    return EvidenceDocument(**{**fields, **changes})


def test_program_keeps_original_publication_date_and_links_exact_race():
    metadata = f"""<script class="yoast-schema-graph" type="application/ld+json">
    {{"@graph":[{{"@type":"WebPage","@id":"{MARATHON_PAGE_URL}",
    "datePublished":"2026-09-15T19:06:54+00:00","dateModified":"2026-11-18T16:13:44+00:00"}}]}}
    </script>"""
    html = _HTML.replace("</head>", metadata + "</head>").replace("<p>", "<h4>Informacije</h4><p>")
    item = parse_marathon_program(html, _KML)
    assert item.published_at == datetime(2026, 9, 15, 19, 6, 54, tzinfo=UTC)
    assert item.published_at_precision == "instant"
    program = EventCandidate(2, item.source_id, item.url, item.title, item.body, item.published_at)
    report = EventCandidate(
        1, "media_danas", "https://www.danas.rs/sport/beogradski-polumaraton-humanitarna-akcija/",
        "Beogradski polumaraton u nedelju", "Sedmi Beogradski polumaraton, koji će se održati "
        "22. novembra, imaće više od 4.000 učesnika.", datetime(2026, 11, 18, 16, 27, tzinfo=UTC),
    )
    assert same_event(report, program)
    assert same_event(program, report)
    assert not same_event(report, replace(
        program, body=item.body.replace("22. novembar", "29. novembar"),
    ))


def test_exports_both_official_lines_and_start_without_archived_course():
    profile = load_city_profile("belgrade")
    snapshot = attach_marathon_kml(_HTML, _KML)
    place = resolve_event_place(_document(), snapshot, profile=profile)

    assert place is not None
    assert place["starts_at"] == "2026-11-22T10:00:00+01:00"
    assert place["ends_at"] == "2026-11-22T14:00:00+01:00"
    assert place["venue"]["longitude"] == 20.421414
    assert place["venue"]["latitude"] == 44.8162991
    assert place["courses"] == [
        {"name": "10 км", "coordinates": [[20.421411, 44.816289], [20.422724, 44.815353]]},
        {"name": "21 км", "coordinates": [
            [20.42141, 44.816288], [20.4059, 44.8228], [20.422692, 44.815324],
        ]},
    ]
    changed = resolve_event_place(
        _document(), attach_marathon_kml(_HTML, _KML.replace("20.4059", "20.406")),
        profile=profile,
    )
    assert changed is not None
    assert changed["courses"] != place["courses"]


@pytest.mark.parametrize(("old", "new"), [
    ("1wLdBTheHVqeu9iZ88dKCzF9QcnQ", "different-map"),
    ("22. novembar 2026.", "23. novembar 2026."),
    ("Start trke: 10:00", "Start trke: 11:00"),
    ("Organizator: Beogradski maraton", "Organizator: druga ustanova"),
])
def test_replacement_page_cannot_attest_old_schedule_or_embedded_map(old, new):
    with pytest.raises(ValueError, match="Официальная карта"):
        attach_marathon_kml(_HTML.replace(old, new), _KML)


@pytest.mark.parametrize(("old", "new"), [
    ("Sava osiguranje Beogradski polumaraton - 10K", "Архив 10K"),
    ("20.4059,44.8228,0", "nan,44.8228,0"),
    ("20.421411,44.816289,0", "20.5,44.9,0"),
    ("<kml ", '<!DOCTYPE kml SYSTEM "https://example.invalid/xxe"><kml '),
])
def test_wrong_or_unsafe_kml_is_rejected_before_snapshot(old, new):
    with pytest.raises(ValueError):
        attach_marathon_kml(_HTML, _KML.replace(old, new))


def test_organizer_schedule_without_collected_map_does_not_create_route():
    profile = load_city_profile("belgrade")
    assert resolve_event_place(_document(), _HTML, profile=profile) is None
    snapshot = attach_marathon_kml(_HTML, _KML)
    assert resolve_event_place(
        _document(source_id="media_danas"), snapshot, profile=profile,
    ) is None
