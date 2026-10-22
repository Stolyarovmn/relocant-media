from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.news_discovery import (
    NewsDiscoveryFeed,
    collect_news_feed,
    load_verified_news_feeds,
    parse_news_feed,
)

REGISTRY = Path("docs/serbian-news-discovery.json")
FIXTURE = Path(__file__).parent / "fixtures" / "rts_news_discovery.xml"
NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)


def test_registry_enables_only_verified_current_feeds() -> None:
    feeds = load_verified_news_feeds(REGISTRY)

    assert feeds == (
        NewsDiscoveryFeed(
            source_id="rts_news",
            source_url="https://www.rts.rs/vesti/",
            feed_url="https://www.rts.rs/vesti/rss.html",
        ),
    )
    assert all("blic.rs" not in feed.feed_url for feed in feeds)


def test_rss_entries_are_discovery_leads_not_evidence() -> None:
    feed = load_verified_news_feeds(REGISTRY)[0]
    xml = FIXTURE.read_text(encoding="utf-8")

    leads = parse_news_feed(xml, feed, NOW)

    assert len(leads) == 1
    lead = leads[0]
    assert lead.source_id == "rts_news"
    assert lead.url.startswith("https://www.rts.rs/vesti/")
    assert lead.requires_independent_verification is True
    assert lead.publication_evidence is False
    assert lead.outbound_urls == (
        "https://www.beograd.rs/lat/beoinfo-vesti/a116299/radovi.html",
    )
    assert "Измена саобраћаја" in lead.text


def test_feed_rejects_xml_entities_and_cross_publisher_articles() -> None:
    feed = load_verified_news_feeds(REGISTRY)[0]
    with pytest.raises(ValueError, match="unsafe news discovery feed"):
        parse_news_feed('<!DOCTYPE x [<!ENTITY y "boom">]><rss></rss>', feed, NOW)

    xml = FIXTURE.read_text(encoding="utf-8")
    leads = parse_news_feed(xml, feed, NOW)
    assert all("evil.example" not in lead.url for lead in leads)


@pytest.mark.asyncio
async def test_collector_allows_only_same_publisher_redirects() -> None:
    feed = load_verified_news_feeds(REGISTRY)[0]
    xml = FIXTURE.read_text(encoding="utf-8")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if str(request.url) == feed.feed_url:
            return httpx.Response(302, headers={"location": "https://rts.rs/vesti/rss.html"})
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        leads = await collect_news_feed(feed, client, now=NOW)

    assert len(leads) == 1
    assert requests == [feed.feed_url, "https://rts.rs/vesti/rss.html"]


@pytest.mark.asyncio
async def test_collector_rejects_cross_publisher_redirect_before_request() -> None:
    feed = load_verified_news_feeds(REGISTRY)[0]
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/rss"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="cross-publisher"):
            await collect_news_feed(feed, client, now=NOW)

    assert requests == [feed.feed_url]
