from pathlib import Path

import pytest

from relocant_media.services.primary_discovery import (
    folded,
    public_search_plan,
    source_for_url,
    topic_matches,
)
from relocant_media.sources.registry import load_source_registry


def sources():
    return {s.id: s for s in load_source_registry(Path('cities/belgrade/sources'))}


def test_serbian_scripts_match_without_a_model_or_extra_facts():
    assert folded('Београдска филхармонија Ренесанс') == 'beogradska filharmonija renesans'
    assert topic_matches('Izložba Nikola Tesla naučnik', 'Изложба Никола Тесла научник')
    assert not topic_matches('Koncert festival Beograd', 'Koncert festival Beograd')


@pytest.mark.parametrize('url', [
    'https://www.kolarac.rs.evil.example/koncerti/a/',
    'https://user:secret@www.kolarac.rs/koncerti/a/',
    'https://www.kolarac.rs:8443/koncerti/a/', 'http://www.kolarac.rs/koncerti/a/',
    'https://127.0.0.1/private',
])
def test_topic_urls_do_not_expand_approved_network_hosts(url):
    assert source_for_url(url, sources()) is None


def test_known_official_site_search_has_one_bounded_term_and_two_site_limit():
    registry = sources()
    aliases = {s: ['Kolarac'] for s in ('venue_kolarac', 'venue_youth_centre', 'venue_musicology')}
    plan = public_search_plan('Kolarac — Ансамбль Ренесанс. Концерт в субботу.', registry, aliases)
    assert len(plan) == 2
    assert all('?s=' in url and 'https%3A' not in url for _, url in plan)
    assert public_search_plan('Нет названия известной площадки', registry, aliases) == []


def test_a_link_to_a_verified_media_article_resolves_to_its_source_but_a_homepage_does_not():
    from pathlib import Path

    from relocant_media.services.primary_discovery import source_for_url
    from relocant_media.sources.registry import load_source_registry

    media = {s.id: s for s in load_source_registry(Path("cities/belgrade/sources"))
             if s.source_type == "news_media"}
    assert set(media) == {"media_rts", "media_n1_srbija", "media_balkan_insight"}

    article = "https://n1info.rs/vesti/neka-vest-o-beogradu/"
    assert source_for_url(article, media) == "media_n1_srbija"
    assert source_for_url("https://www.rts.rs/lat/vesti/drustvo/123/vest.html",
                          media) == "media_rts"
    assert source_for_url("https://balkaninsight.com/2026/10/13/some-story/",
                          media) == "media_balkan_insight"
    # Not articles, not verified hosts, not https, and credential tricks are all refused.
    assert source_for_url("https://n1info.rs/", media) is None
    assert source_for_url("https://n1info.rs/vesti/", media) is None
    assert source_for_url("https://evil.example/vesti/vest/", media) is None
    assert source_for_url("http://n1info.rs/vesti/neka-vest/", media) is None
    assert source_for_url("https://user@n1info.rs/vesti/neka-vest/", media) is None
    assert source_for_url("https://n1info.rs.evil.example/vesti/neka-vest/", media) is None
