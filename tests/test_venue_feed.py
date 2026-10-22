from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.venue_feed import VenueFeedCollector, parse_venue_feed
from relocant_media.sources.registry import ExtractionConfig, SourceConfig, load_source_registry


@pytest.fixture
def source() -> SourceConfig:
    original = load_source_registry(Path("cities/belgrade/sources"))[0]
    return original.model_copy(update={
        "url": httpx.URL("https://museum.example/feed/"),
        "extraction": ExtractionConfig(strategy="venue_feed"),
    })


def feed(date: str, title: str = "Exhibition", extra: str = "") -> str:
    return (f"<rss><channel><item><title>{title}</title>"
            f"<link>https://museum.example/event/</link><pubDate>{date}</pubDate>"
            f"<description>Teaser</description>{extra}</item></channel></rss>")


def test_discovery_never_uses_teaser_as_evidence(source: SourceConfig) -> None:
    item = parse_venue_feed(feed("Tue, 20 Oct 2026 12:00:00 +0000"), source)[0]
    assert item.body == ""
    assert item.published_at == datetime(2026, 10, 20, 12, tzinfo=UTC)


@pytest.mark.parametrize("xml", [
    '<!DOCTYPE rss [<!ENTITY x "bad">]><rss/>',
    "<!ENTITY x SYSTEM 'file:///etc/passwd'><rss/>",
])
def test_dtd_rejected(source: SourceConfig, xml: str) -> None:
    with pytest.raises(ValueError):
        parse_venue_feed(xml, source)


def test_updated_is_not_publication(source: SourceConfig) -> None:
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title>'
           '<link href="https://museum.example/event/"/>'
           '<updated>2026-10-20T12:00:00Z</updated></entry></feed>')
    assert parse_venue_feed(xml, source) == []


@pytest.mark.parametrize("date,title", [("unknown", "A"), ("2026-10-20T12:00:00", "A"),
                                        ("2026-10-20T12:00:00Z", "")])
def test_missing_title_or_real_date_is_skipped(source: SourceConfig, date: str, title: str) -> None:
    assert parse_venue_feed(feed(date, title), source) == []


@pytest.mark.asyncio
async def test_cross_host_redirect_not_requested(source: SourceConfig) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})

    async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await VenueFeedCollector(source, client).collect()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_full_article_and_stable_feed_date(source: SourceConfig) -> None:
    date = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    article = "<h1>Full title</h1><article>" + "Verified full announcement. " * 12 + "</article>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed(date.isoformat()) if request.url.path == "/feed/"
                              else article)

    async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(handler)) as client:
        collector = VenueFeedCollector(source, client)
        items = await collector.collect()
        live = await collector.collect_url(items[0].url)
    assert items == [live]
    assert live.title == "Full title"
    assert live.published_at == date
    assert "Verified full announcement" in live.body


@pytest.mark.asyncio
async def test_teaser_article_is_not_full_evidence(source: SourceConfig) -> None:
    async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="<h1>Title</h1><article>Short</article>")
    )) as client:
        with pytest.raises(ValueError):
            await VenueFeedCollector(source, client).collect_url("https://museum.example/event/")


@pytest.mark.asyncio
async def test_old_entries_do_not_fetch_articles(source: SourceConfig) -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=feed("Tue, 01 Jan 2019 12:00:00 +0000"))

    async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(handler)) as client:
        assert await VenueFeedCollector(source, client).collect() == []
    assert len(requested) == 1


@pytest.mark.asyncio
async def test_encoded_full_content_is_live_evidence(source: SourceConfig) -> None:
    date = datetime.now(UTC) - timedelta(hours=1)
    xml = feed(date.isoformat(), extra=(
        '<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        '<![CDATA[<p>' + 'Full first party event announcement. ' * 10 +
        '</p>]]></content:encoded>'
    ))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(handler)) as client:
        collector = VenueFeedCollector(source, client)
        item = (await collector.collect())[0]
        assert await collector.collect_url(item.url) == item
    assert all(url.endswith('/feed/') for url in requests)
    assert 'Full first party' in item.body


def test_future_pubdate_is_not_announcement_date(source: SourceConfig) -> None:
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    assert parse_venue_feed(feed(future), source) == []


@pytest.mark.asyncio
async def test_bad_article_does_not_hide_other_fresh_announcements(source: SourceConfig) -> None:
    date = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    xml = feed(date).replace('</channel>',
        f'<item><title>Concert</title><link>https://museum.example/good/</link>'
        f'<pubDate>{date}</pubDate></item></channel>')

    def handler(request: httpx.Request) -> httpx.Response:
        text = xml if request.url.path == '/feed/' else (
            '<h1>Concert 31. 10. 2026.</h1><article>' + 'Complete verified details. ' * 10
            + '</article>' if request.url.path == '/good/' else '<h1>Teaser</h1>')
        return httpx.Response(200, text=text)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = VenueFeedCollector(source, client)
        items = await collector.collect()
        assert len(items) == 1 and items[0].url.endswith('/good/')
        assert collector.item_failures == [dict(
            url='https://museum.example/event/',
            error_type='ValueError',
            error_message='venue article body shorter than 120 characters',
        )]
        # Discovery skips an unusable page; a live evidence check still fails closed.
        with pytest.raises(ValueError):
            await collector.collect_url('https://museum.example/event/')


@pytest.mark.asyncio
async def test_all_bad_fresh_articles_are_a_failure_not_empty_success(source: SourceConfig) -> None:
    date = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    def handler(request: httpx.Request) -> httpx.Response:
        text = feed(date) if request.url.path == '/feed/' else '<h1>X</h1>'
        return httpx.Response(200, text=text)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = VenueFeedCollector(source, client)
        with pytest.raises(ValueError, match='all fresh venue articles'):
            await collector.collect()
        assert len(collector.item_failures) == 1
        assert collector.item_failures[0]['error_message'] == (
            'venue article body shorter than 120 characters'
        )


@pytest.mark.asyncio
async def test_jsonld_publication_date_when_entry_has_disappeared(source: SourceConfig) -> None:
    article = ('<script type="application/ld+json">'
               '{"@type":"Article","headline":"Full title",'
               '"datePublished":"2026-10-01T10:15:00+02:00",'
               '"articleBody":"' + 'Complete verified announcement text. ' * 10 + '"}</script>')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<rss><channel/></rss>'
                              if request.url.path == '/feed/' else article)

    async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(handler)) as client:
        item = await VenueFeedCollector(source, client).collect_url('https://museum.example/event/')
    assert item.published_at == datetime(2026, 10, 1, 8, 15, tzinfo=UTC)
    assert item.title == 'Full title'


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_lead', [False, True])
async def test_configured_header_lead_requires_article_not_encoded_feed(
    source: SourceConfig, missing_lead: bool,
) -> None:
    """Dom omladine puts concert time in a header omitted from content:encoded."""
    source = source.model_copy(update={'extraction': source.extraction.model_copy(update={
        'article_selectors': ('.entry-content',),
        'article_lead_selectors': ('article .entry-header .entry-excerpt',),
    })})
    date = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    fixture = Path('tests/fixtures/venue_header_lead.html').read_text(encoding='utf-8')
    if missing_lead:
        fixture = fixture.replace('class="entry-excerpt"', 'class="changed-template"')
    xml = feed(date.isoformat(), extra=(
        '<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        '<![CDATA[<p>' + 'Interview body without the date or venue. ' * 8 +
        '</p>]]></content:encoded>'
    ))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, text=xml if request.url.path == '/feed/' else fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = VenueFeedCollector(source, client)
        if missing_lead:
            with pytest.raises(ValueError, match='all fresh venue articles'):
                await collector.collect()
            assert collector.item_failures[0]['error_message'] == (
                'venue article lacks configured lead'
            )
            with pytest.raises(ValueError, match='configured lead'):
                await collector.collect_url('https://museum.example/event/')
        else:
            item = (await collector.collect())[0]
            assert await collector.collect_url(item.url) == item
            assert item.body.startswith('Concert on 22 October at 19:00 in the main hall.')
            assert 'complete interview body' in item.body
            assert 'Unrelated concert' not in item.body
            assert item.published_at == date
    assert '/event/' in requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("article", "expected"),
    [
        (
            "<article>" + "Complete verified announcement. " * 10 + "</article>",
            "venue article title missing",
        ),
        (
            "<h1>Full title</h1><article>Short</article>",
            "venue article body shorter than 120 characters",
        ),
    ],
)
async def test_article_validation_reasons_are_specific_with_feed_fallback(
    source: SourceConfig, article: str, expected: str,
) -> None:
    date = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=feed(date) if request.url.path == '/feed/' else article)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match=expected):
            await VenueFeedCollector(source, client).collect_url('https://museum.example/event/')


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("date_published", "expected"),
    [
        (None, "venue article publication date missing"),
        (
            (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "venue article publication date is in the future",
        ),
    ],
)
async def test_article_publication_date_reasons_are_specific_without_feed_fallback(
    source: SourceConfig, date_published: str | None, expected: str,
) -> None:
    date_field = f',"datePublished":"{date_published}"' if date_published else ""
    article = (
        '<script type="application/ld+json">'
        '{"@type":"Article","headline":"Full title","articleBody":"'
        + 'Complete verified announcement. ' * 10
        + '"'
        + date_field
        + '}</script>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<rss><channel/></rss>'
                              if request.url.path == '/feed/' else article)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match=expected):
            await VenueFeedCollector(source, client).collect_url('https://museum.example/event/')
