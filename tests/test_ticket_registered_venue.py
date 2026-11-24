from datetime import UTC, datetime
from pathlib import Path

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.ticket_vision_events import parse_ticket_event
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.services.event_place import resolve_event_place
from relocant_media.sources.registry import load_source_file

_TICKET = "https://tickets.rs/event/artist_band_27517"
_HTML = f"""
<link rel="canonical" href="{_TICKET}">
<h1>MUSIC ARTIST + GUEST BAND</h1>
<div class="selected-event">
  <p><strong>MUSIC ARTIST + GUEST BAND</strong></p>
  <p><strong>Luka Beograd, Beograd</strong></p>
  <p><strong>25. novembar 2026, sreda 19:00</strong></p>
</div>
"""
_ARTICLE = EvidenceDocument(
    source_id="media_danas", url="https://danas.rs/kultura/artist-band/",
    title="Music Artist + Guest Band u Beogradu",
    body="Koncert će se održati 25. novembra 2026. godine u Hangaru Luke Beograd. "
         f"Ulaznice su u prodaji {_TICKET}.",
    published_at=datetime(2026, 11, 24, 8, 29, tzinfo=UTC),
)


def _resolve(article: EvidenceDocument, html: str = _HTML):
    source = load_source_file(Path("cities/belgrade/sources/ticket_vision_events.yaml"))
    ticket = parse_ticket_event(html, _TICKET, source)
    document = EvidenceDocument.model_validate(ticket, from_attributes=True)
    return resolve_event_place(
        document, html, profile=load_city_profile("belgrade"), media=(article,),
    )


def test_ticket_schedule_and_independently_registered_venue_produce_a_map():
    place = _resolve(_ARTICLE)
    assert place["venue"]["name"] == "Hangar Luka Beograd"
    assert place["venue"]["address"] == "Dunavska 22, Beograd"
    assert place["venue"]["latitude"] == 44.8259913
    assert place["schedule_source_url"] == _TICKET
    assert place["coordinate_source_url"] == "https://www.openstreetmap.org/node/13257326394"
    assert place["starts_at"] == "2026-11-25T19:00:00+01:00"
    assert place["ends_at"] is None


@pytest.mark.parametrize(("old", "new"), [
    ("u Hangaru Luke Beograd", "u Luci Beograd"),
    ("u Hangaru Luke Beograd", "u Luci Beograd. Drugi koncert u Hangaru Luke Beograd"),
    ("25. novembra", "26. novembra"),
    (_TICKET, "https://tickets.rs/event/another_band_27518"),
    ("Music Artist + Guest Band", "Music Artist + Other Performers"),
])
def test_ticket_map_rejects_unspecific_venue_and_mismatched_event(old, new):
    article = _ARTICLE.model_copy(update={
        "title": _ARTICLE.title.replace(old, new), "body": _ARTICLE.body.replace(old, new),
    })
    assert _resolve(article) is None
