import json

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import PackageEventDocument
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.services.event_place import event_time, resolve_event_place

URL = "https://example.org/concert"
PROFILE = load_city_profile("belgrade")


def document():
    return EvidenceDocument(
        source_id="test_venue",
        url=URL,
        title="Mali koncert",
        body="Mali koncert u Test sala. 2026-11-16 u 20:00.",
    )


def html(**changes):
    node = {
        "@context": "https://schema.org",
        "@type": "MusicEvent",
        "url": URL,
        "name": "Mali koncert",
        "startDate": "2026-11-16T20:00:00+01:00",
        "location": {
            "@type": "Place",
            "name": "Test sala",
            "address": {
                "addressLocality": "Beograd",
                "addressCountry": "RS",
                "streetAddress": "Primer 1",
            },
            "geo": {"@type": "GeoCoordinates", "latitude": "44.81", "longitude": "20.46"},
        },
    }
    node.update(changes)
    return '<script type="application/ld+json">' + json.dumps(node) + "</script>"


def test_explicit_event_without_end_gets_cutoff_not_fictional_duration():
    place = resolve_event_place(document(), html(), profile=PROFILE)
    assert place is not None
    assert place["starts_at"] == "2026-11-16T20:00:00+01:00"
    assert place["ends_at"] is None
    assert place["expires_at"] == "2026-11-17T00:00:00+01:00"
    assert place == resolve_event_place(document(), html(), profile=PROFILE)


def test_base_and_package_document_share_hash_and_result():
    base = document()
    extended = PackageEventDocument(
        **base.model_dump(), raw_item_id=2, ownership="official", high_risk=False, auto_publish=True
    )
    assert resolve_event_place(base, html(), profile=PROFILE) == resolve_event_place(
        extended, html(), profile=PROFILE
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"startDate": "2026-11-16"},
        {"startDate": "2026-11-16T20:00:00+02:00"},
        {"startDate": "2026-03-29T02:30:00"},
        {"startDate": "2026-10-25T02:30:00"},
        {"endDate": "2026-11-16T19:00:00+01:00"},
        {"endDate": "2026-11-17"},
        {"eventStatus": "https://schema.org/EventCancelled"},
        {"eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode"},
        {"url": "https://example.org/another"},
        {"location": None},
        {"name": "Other event"},
    ],
)
def test_missing_ambiguous_or_conflicting_facts_do_not_make_a_place(changes):
    assert resolve_event_place(document(), html(**changes), profile=PROFILE) is None


def test_two_matching_events_are_ambiguous():
    assert resolve_event_place(document(), html() + html(), profile=PROFILE) is None


def test_timezone_aware_end_is_preserved():
    place = resolve_event_place(
        document(), html(endDate="2026-11-16T22:30:00+01:00"), profile=PROFILE
    )
    assert place["ends_at"] == place["expires_at"] == "2026-11-16T22:30:00+01:00"
    assert event_time("2026-11-16T20:00:00", "Europe/Belgrade").hour == 20
