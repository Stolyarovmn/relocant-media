from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.official_guide import OfficialGuideCollector, parse_official_guide
from relocant_media.domain.freshness import evidence_freshness, guide_freshness
from relocant_media.ops.server_cycle import guide_sources, media_sources, venue_sources
from relocant_media.sources.registry import load_source_registry

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
SOURCES = Path("cities/belgrade/sources")
HTML = Path("tests/fixtures/guide_nsz_foreigners.html").read_text(encoding="utf-8")


def _source():
    return next(
        s for s in load_source_registry(SOURCES) if s.id == "guide_nsz_foreigner_employment"
    )


@pytest.mark.parametrize(
    ("age_days", "action", "reason"),
    [(0, "keep", "guide_verified"), (29, "keep", "guide_verified"),
     (31, "discard", "guide_verification_expired")],
)
def test_guide_is_valid_for_thirty_days_after_it_was_first_seen(age_days, action, reason):
    decision = guide_freshness(NOW - timedelta(days=age_days), NOW)
    assert (decision.action, decision.reason) == (action, reason)


def test_guide_without_a_verification_date_is_held_and_never_future_dated():
    assert guide_freshness(None, NOW).action == "hold"
    assert guide_freshness(NOW + timedelta(hours=1), NOW).action == "hold"


def test_news_keeps_its_own_freshness_rules_and_guides_ignore_publication_date():
    news = evidence_freshness("Naslov", "Telo", None, NOW, precision="unknown",
                              source_type="official_city_news", verified_at=NOW)
    assert (news.action, news.reason) == ("hold", "publication_date_unknown")
    guide = evidence_freshness("Naslov", "Telo", None, NOW, precision="unknown",
                               source_type="official_guide", verified_at=NOW - timedelta(days=3))
    assert guide.action == "keep"


def test_guide_sources_are_official_review_only_and_isolated():
    registry = {s.id: s for s in load_source_registry(SOURCES)}
    assert set(guide_sources()) >= {"guide_nsz_foreigner_employment"}
    for source_id in guide_sources():
        source = registry[source_id]
        assert source.ownership == "official" and source.source_type == "official_guide"
        assert source.policy.auto_publish is False
        assert source.extraction.strategy == "official_guide"
    assert not set(guide_sources()) & (set(media_sources()) | set(venue_sources()))


def test_parser_returns_undated_text_with_a_content_bound_identity():
    item = parse_official_guide(HTML, _source())
    assert item.published_at is None and len(item.body) >= 800
    assert item.title == "Employment of Foreigners in the Republic of Serbia"
    assert "menu menu" not in item.body and "footer text" not in item.body
    changed = parse_official_guide(HTML.replace("Serbia", "Serbia today", 3), _source())
    assert changed.external_id != item.external_id
    assert parse_official_guide(HTML, _source()).external_id == item.external_id


def test_page_with_too_little_text_is_rejected_instead_of_published():
    with pytest.raises(ValueError, match="too short"):
        parse_official_guide("<html><body><article><h1>T</h1><p>short</p></article></body></html>",
                             _source())


@pytest.mark.asyncio
async def test_redirect_to_an_unapproved_host_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.nsz.gov.rs":
            return httpx.Response(302, headers={"location": "https://evil.example/page"})
        raise AssertionError("must not follow the redirect")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="host"):
            await OfficialGuideCollector(_source(), client).collect()


@pytest.mark.asyncio
async def test_collector_fetches_the_official_page_once():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        items = await OfficialGuideCollector(_source(), client).collect()
    assert len(items) == 1 and len(calls) == 1
