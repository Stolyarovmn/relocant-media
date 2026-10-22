from pathlib import Path

from relocant_media.sources.registry import load_source_registry


def test_belgrade_source_registry_loads_and_has_unique_ids() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    assert len(sources) == 24
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


def test_utility_sources_keep_shadow_policy_while_bvk_and_eds_are_structured() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    utility_sources = [s for s in sources if s.source_type == "official_utility_service_info"]

    assert {source.id for source in utility_sources} == {
        "bvk_planned_works",
        "eds_belgrade_outages_today",
        "eds_belgrade_outages_tomorrow",
        "eds_belgrade_outages_day2",
        "eds_belgrade_outages_day3",
    }
    bvk = next(source for source in utility_sources if source.id == "bvk_planned_works")
    eds = [source for source in utility_sources if source.id != "bvk_planned_works"]
    assert bvk.extraction.strategy == "bvk_planned_works"
    assert all(source.extraction.strategy == "eds_belgrade_outages" for source in eds)
    assert all(source.ownership == "official" for source in utility_sources)
    assert all(source.trust == 1.0 for source in utility_sources)
    assert all(source.extraction.status == "active" for source in utility_sources)
    assert all(source.policy.auto_publish is False for source in utility_sources)
    assert all(source.verification.checked_at is not None for source in utility_sources)


def test_parking_and_heating_sources_are_verified_shadow_snapshots() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    selected = [
        source
        for source in sources
        if source.id in {"parking_servis_news", "beoelektrane_network_works"}
    ]

    assert len(selected) == 2
    assert all(source.ownership == "official" for source in selected)
    assert all(source.trust == 1.0 for source in selected)
    assert all(source.extraction.strategy == "page_change_detection" for source in selected)
    assert all(source.extraction.status == "active" for source in selected)
    assert all(source.policy.auto_publish is False for source in selected)
    assert all(source.verification.checked_at is not None for source in selected)


def test_new_vertical_sources_remain_shadow_only() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    rail = next(source for source in sources if source.id == "srbijavoz_news")
    residence = next(
        source for source in sources if source.id == "welcome_serbia_temporary_residence"
    )

    assert rail.ownership == "official"
    assert rail.trust == 1.0
    assert rail.extraction.strategy == "page_change_detection"
    assert rail.extraction.status == "active"
    assert rail.policy.high_risk is False
    assert rail.policy.auto_publish is False
    assert str(rail.url) == "https://srbijavoz.rs/vesti/"

    assert residence.ownership == "official"
    assert residence.trust == 1.0
    assert residence.extraction.strategy == "page_change_detection"
    assert residence.extraction.status == "active"
    assert residence.policy.high_risk is True
    assert residence.policy.auto_publish is False
    assert str(residence.url) == "https://welcometoserbia.gov.rs/privremeni-boravak"
