from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors import venue_feed
from relocant_media.sources.registry import load_source_registry

FIXTURE = Path("tests/fixtures/media_rts_article.html")
ARTICLE_URL = "https://www.rts.rs/vesti/drustvo/123/synthetic.html"


class _Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 2, 10, tzinfo=UTC).astimezone(tz)


@pytest.fixture
def rts_source(monkeypatch):
    monkeypatch.setattr(venue_feed, "datetime", _Clock)
    return next(source for source in load_source_registry(Path("cities/belgrade/sources"))
                if source.id == "media_rts")


def _feed() -> str:
    # The feed timestamp describes the update, not the original publication.
    return ("<rss><channel><item><title>Feed title</title><link>" + ARTICLE_URL
            + "</link><pubDate>Sat, 31 Oct 2026 15:47:00 +0000</pubDate>"
            + '<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            + "<![CDATA[<p>" + "Feed-only evidence must not be used. " * 8
            + "</p>]]></content:encoded></item></channel></rss>")


@pytest.mark.asyncio
async def test_rts_page_evidence_stays_identical_after_feed_entry_disappears(rts_source):
    article = FIXTURE.read_text(encoding="utf-8")
    feed_reads = 0
    article_reads = 0

    def handler(request):
        nonlocal feed_reads, article_reads
        if str(request.url) == rts_source.extraction.feed_url:
            feed_reads += 1
            return httpx.Response(200, text=_feed() if feed_reads == 1
                                  else "<rss><channel/></rss>")
        assert str(request.url) == ARTICLE_URL
        article_reads += 1
        return httpx.Response(200, text=article)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = venue_feed.VenueFeedCollector(rts_source, client)
        collected = await collector.collect()
        live = await collector.collect_url(ARTICLE_URL)
    assert collected == [live]
    assert feed_reads == article_reads == 2
    assert live.published_at == datetime(2026, 10, 31, 7, 21, tzinfo=UTC)
    assert live.title == "Синтетическая новость для проверки коллектора"
    assert live.body.startswith("Синтетический лид с существенными условиями объявления.")
    assert "Синтетический полный текст" in live.body
    assert "Посторонняя новость" not in live.body
    assert "Feed-only" not in live.body


@pytest.mark.asyncio
@pytest.mark.parametrize("date", ["", "субота, 31.02.2026, 08:21 -> 16:47",
                                   "субота, 31.10.2026, -> 16:47",
                                   "субота, 31.10.2026, 99:99 -> 16:47"])
async def test_rts_requires_explicit_valid_original_time_even_with_other_dates(rts_source, date):
    article = FIXTURE.read_text(encoding="utf-8").replace(
        "субота, 31.10.2026, 08:21 -&gt; 16:47", date)
    # Neither a generic metadata date nor the RSS timestamp may replace the RTS label.
    article = article.replace("<html>", '<html><head><meta property="article:published_time" '
                              'content="2026-10-31T15:47:00Z"></head>')

    def handler(request):
        return httpx.Response(200, text=_feed()
                              if str(request.url) == rts_source.extraction.feed_url else article)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = venue_feed.VenueFeedCollector(rts_source, client)
        with pytest.raises(ValueError, match="all fresh venue articles"):
            await collector.collect()
        assert collector.item_failures[0]["error_message"] == (
            "RTS article publication date missing or invalid")
        with pytest.raises(ValueError, match="RTS article publication date missing or invalid"):
            await collector.collect_url(ARTICLE_URL)
