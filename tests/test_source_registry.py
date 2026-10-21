from pathlib import Path

from relocant_media.sources.registry import load_source_registry


def test_belgrade_source_registry_loads_and_has_unique_ids() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    assert len(sources) == 20
    assert len({source.id for source in sources}) == len(sources)

    euprava = next(source for source in sources if source.id == "euprava_foreigner_registration")
    assert euprava.policy.high_risk is True
    assert euprava.policy.auto_publish is False
    assert euprava.extraction.strategy == "page_change_detection"

    news = next(source for source in sources if source.id == "city_beograd_news")
    assert news.extraction.strategy == "beograd_news_list"
    assert str(news.url) == "https://www.beograd.rs/lat/vesti"


def test_venue_sources_are_explicit_first_party_events():
    sources = load_source_registry(Path("cities/belgrade/sources"))
    venues = [s for s in sources if s.source_type == "official_venue_events"]
    assert len(venues) == 7
    assert all(s.ownership == "official" and s.categories == ("events",) for s in venues)
    assert all(s.extraction.feed_url and s.extraction.status == "active" for s in venues)
    assert all(s.verification.checked_at is not None for s in venues)


def test_utility_sources_are_verified_shadow_snapshots() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    utility_sources = [s for s in sources if s.source_type == "official_utility_service_info"]

    assert {source.id for source in utility_sources} == {
        "bvk_planned_works",
        "eds_belgrade_outages_today",
        "eds_belgrade_outages_tomorrow",
        "eds_belgrade_outages_day2",
        "eds_belgrade_outages_day3",
    }
    assert all(source.ownership == "official" for source in utility_sources)
    assert all(source.trust == 1.0 for source in utility_sources)
    assert all(source.extraction.strategy == "page_change_detection" for source in utility_sources)
    assert all(source.extraction.status == "active" for source in utility_sources)
    assert all(source.policy.auto_publish is False for source in utility_sources)
    assert all(source.verification.checked_at is not None for source in utility_sources)
