from pathlib import Path

import pytest

from relocant_media.collectors.venue_feed import parse_venue_feed
from relocant_media.domain.originality import copies_source_text
from relocant_media.domain.policy import primary_evidence_kind
from relocant_media.ops.server_cycle import discovery_venue_sources, media_sources, venue_sources
from relocant_media.sources.registry import load_source_registry

SOURCES = Path("cities/belgrade/sources")
MEDIA = {
    "media_rts": "media_rts_feed.xml",
    "media_n1_srbija": "media_n1_feed.xml",
    "media_balkan_insight": "media_balkan_insight_feed.xml",
}


def _registry():
    return {source.id: source for source in load_source_registry(SOURCES)}


def test_verified_media_are_review_only_non_high_risk_sources() -> None:
    registry = _registry()
    assert set(media_sources()) == set(MEDIA)
    for source_id in MEDIA:
        source = registry[source_id]
        assert source.ownership == "media" and source.source_type == "news_media"
        assert source.policy.auto_publish is False and source.policy.high_risk is False
        assert source.extraction.strategy == "venue_feed" and source.extraction.status == "active"
        assert 0 < source.trust <= 0.85
        host = str(source.extraction.feed_url).split("/")[2]
        assert host in source.extraction.approved_hosts


def test_media_never_enter_the_official_venue_paths() -> None:
    assert not set(MEDIA) & set(venue_sources())
    assert not set(MEDIA) & set(discovery_venue_sources())


@pytest.mark.parametrize(("source_id", "fixture"), MEDIA.items())
def test_real_feed_samples_yield_full_text_items(source_id: str, fixture: str) -> None:
    xml = (Path("tests/fixtures") / fixture).read_text(encoding="utf-8")
    items = parse_venue_feed(xml, _registry()[source_id])
    assert items and all(len(item.body) >= 120 and item.published_at for item in items)


def test_media_can_only_support_non_high_risk_topics() -> None:
    assert primary_evidence_kind(["Official", "media"], "law") == "official"
    assert primary_evidence_kind(["media"], "events") == "media"
    assert primary_evidence_kind([" Media "], "transport") == "media"
    assert primary_evidence_kind(["media"], "politics") is None
    assert primary_evidence_kind(["media"], "LAW") is None
    assert primary_evidence_kind(["unknown"], "events") is None


def test_verbatim_copy_is_detected_but_translation_and_retelling_pass() -> None:
    source = ("Gradska vlada je saopštila da će od ponedeljka biti obustavljen saobraćaj "
              "u ulici Kralja Milana zbog radova na vodovodnoj mreži u trajanju od tri dana")
    copied = "Важно: " + source + " — сообщили власти."
    assert copies_source_text(copied, [source]) is True
    retold = ("С понедельника на три дня перекроют улицу Кралja Милана: идёт ремонт водопровода, "
              "власти советуют ехать в объезд.")
    assert copies_source_text(retold, [source]) is False
    assert copies_source_text("", [source]) is False
    assert copies_source_text(copied, []) is False
