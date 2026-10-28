from pathlib import Path

from relocant_media.sources.registry import load_source_registry


def test_belgrade_source_registry_loads_and_has_unique_ids() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    assert len(sources) == 31
    assert len({source.id for source in sources}) == len(sources)

    euprava = next(source for source in sources if source.id == "euprava_foreigner_registration")
    assert euprava.policy.high_risk is True
    assert euprava.policy.auto_publish is False
    assert euprava.extraction.strategy == "euprava_service"
    assert euprava.extraction.status == "active"
    assert euprava.verification.checked_at is not None

    news = next(source for source in sources if source.id == "city_beograd_news")
    assert news.extraction.strategy == "beograd_news_list"
    assert str(news.url) == "https://www.beograd.rs/lat/vesti"

    government = next(source for source in sources if source.id == "srbija_gov_news")
    assert government.ownership == "official"
    assert government.trust == 1.0
    assert government.extraction.strategy == "srbija_gov_article"
    assert government.extraction.status == "active"
    assert government.policy.high_risk is True
    # Category/source risk is the publication gate. Setting this false would stop the item
    # before the owner-review path with source_auto_publish_disabled.
    assert government.policy.auto_publish is True
    assert government.verification.checked_at is not None
    assert str(government.url) == "https://www.srbija.gov.rs/vesti/239/"


def test_venue_sources_are_explicit_first_party_events():
    sources = load_source_registry(Path("cities/belgrade/sources"))
    venues = [s for s in sources if s.source_type == "official_venue_events"]
    assert len(venues) == 7
    assert all(s.ownership == "official" and s.categories == ("events",) for s in venues)
    assert all(s.verification.checked_at is not None for s in venues)
    assert all(s.extraction.status == "active" for s in venues)

    natural_history = next(s for s in venues if s.id == "venue_natural_history")
    yugoslavia = next(s for s in venues if s.id == "venue_yugoslavia")

    assert natural_history.extraction.strategy == "natural_history_news"
    assert natural_history.extraction.feed_url is None
    assert natural_history.extraction.approved_hosts == (
        "nhmbeo.rs",
        "www.nhmbeo.rs",
    )
    assert natural_history.policy.auto_publish is False
    assert str(natural_history.url) == "https://nhmbeo.rs/vesti/"

    assert yugoslavia.extraction.strategy == "yugoslavia_programs"
    assert yugoslavia.extraction.feed_url is None
    assert yugoslavia.extraction.approved_hosts == (
        "muzej-jugoslavije.org",
        "www.muzej-jugoslavije.org",
    )
    assert yugoslavia.policy.auto_publish is False
    assert str(yugoslavia.url) == "https://muzej-jugoslavije.org/rs/kalendar/"


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


def test_parking_stays_snapshot_while_heating_is_structured_shadow() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    parking = next(source for source in sources if source.id == "parking_servis_news")
    heating = next(source for source in sources if source.id == "beoelektrane_network_works")

    assert parking.ownership == "official"
    assert parking.trust == 1.0
    assert parking.extraction.strategy == "page_change_detection"
    assert parking.extraction.status == "active"
    assert parking.policy.auto_publish is False

    assert heating.ownership == "official"
    assert heating.trust == 1.0
    assert heating.extraction.strategy == "beoelektrane_network_works"
    assert heating.extraction.status == "active"
    assert heating.policy.high_risk is False
    assert heating.policy.auto_publish is False
    assert heating.verification.checked_at is not None


def test_bgprevoz_changes_is_structured_shadow_without_publication_date_inference() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    transport = next(source for source in sources if source.id == "bgprevoz_changes")

    assert transport.ownership == "official"
    assert transport.trust == 1.0
    assert transport.extraction.strategy == "bgprevoz_changes"
    assert transport.extraction.status == "active"
    assert transport.extraction.approved_hosts == ("bgprevoz.rs",)
    assert transport.policy.high_risk is False
    assert transport.policy.auto_publish is False
    assert transport.verification.checked_at is not None


def test_bgprevoz_news_is_structured_shadow_with_explicit_redirect_host() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    news = next(source for source in sources if source.id == "bgprevoz_news")

    assert news.ownership == "official"
    assert news.trust == 1.0
    assert news.extraction.strategy == "bgprevoz_news"
    assert news.extraction.status == "active"
    assert news.extraction.approved_hosts == ("www.bgprevoz.rs",)
    assert news.policy.high_risk is False
    assert news.policy.auto_publish is False
    assert news.verification.checked_at is not None


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
    assert residence.extraction.strategy == "welcome_serbia_guidance"
    assert residence.extraction.status == "active"
    assert residence.policy.high_risk is True
    assert residence.policy.auto_publish is False
    assert residence.verification.checked_at is not None
    assert str(residence.url) == "https://welcometoserbia.gov.rs/privremeni-boravak"
