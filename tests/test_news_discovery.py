from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.base import ArticleBatchError
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

    observation: dict = {}
    leads = parse_news_feed(xml, feed, NOW, observation=observation)

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
    assert observation["status"] == "partial"
    assert observation["fresh_items"] == 1
    assert observation["fetched_items"] == 3
    assert [item["url"] for item in observation["item_failures"]] == [feed.feed_url]


def test_feed_rejects_xml_entities_and_cross_publisher_articles() -> None:
    feed = load_verified_news_feeds(REGISTRY)[0]
    with pytest.raises(ValueError, match="unsafe news discovery feed"):
        parse_news_feed('<!DOCTYPE x [<!ENTITY y "boom">]><rss></rss>', feed, NOW)

    xml = FIXTURE.read_text(encoding="utf-8")
    leads = parse_news_feed(xml, feed, NOW)
    assert all("evil.example" not in lead.url for lead in leads)


@pytest.mark.parametrize(("entry", "status"), [
    ("", "empty_index"),
    ("<item><title>Old</title><link>https://www.rts.rs/vesti/a.html</link>"
     "<pubDate>Sun, 18 Oct 2026 10:00:00 +0000</pubDate></item>", "empty_fresh"),
])
def test_feed_distinguishes_empty_index_from_old_entries(entry, status):
    feed = load_verified_news_feeds(REGISTRY)[0]
    xml = f'<rss version="2.0"><channel><title>РТС</title>{entry}</channel></rss>'
    observation: dict = {}
    assert parse_news_feed(xml, feed, NOW, observation=observation) == []
    assert observation["status"] == status and observation["item_failures"] == []


def test_feed_reports_all_bad_records_without_revealing_links():
    feed = load_verified_news_feeds(REGISTRY)[0]
    xml = ('<rss version="2.0"><channel><title>РТС</title>'
           '<item><title>Bad</title><link>https://evil.example/private</link></item>'
           '</channel></rss>')
    observation: dict = {}
    with pytest.raises(ArticleBatchError) as caught:
        parse_news_feed(xml, feed, NOW, observation=observation)
    assert observation["status"] == "error"
    assert observation["item_failures"] == caught.value.item_failures
    assert observation["item_failures"][0]["url"] == feed.feed_url
    assert "evil.example" not in repr(observation)


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
