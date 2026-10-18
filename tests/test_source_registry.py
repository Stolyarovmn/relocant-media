from pathlib import Path

from relocant_media.sources.registry import load_source_registry


def test_belgrade_source_registry_loads_and_has_unique_ids() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    assert len(sources) == 7
    assert len({source.id for source in sources}) == len(sources)

    euprava = next(source for source in sources if source.id == "euprava_foreigner_registration")
    assert euprava.policy.high_risk is True
    assert euprava.policy.auto_publish is False
    assert euprava.extraction.strategy == "page_change_detection"

    news = next(source for source in sources if source.id == "city_beograd_news")
    assert news.extraction.strategy == "beograd_news_list"
    assert str(news.url) == "https://www.beograd.rs/lat/vesti"
