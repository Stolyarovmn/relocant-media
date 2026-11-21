from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.tob_events import TobEventsCollector, parse_tob_event
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/tob_events.yaml")


def _source():
    return load_source_file(SOURCE_PATH)


def _list_html() -> str:
    return """
    <html><body>
      <header><h1>Turistička organizacija Beograda</h1></header>
      <h1>Događaji</h1>
      <article><h2><a href="/sr/events/yasmin-levi">YASMIN LEVI</a></h2></article>
      <article><h2><a href="/sr/events/andre-riju">Andre Riju</a></h2></article>
      <h1>Ne propustite</h1>
      <a href="/sr/events/duplicate-recommendation">Recommendation</a>
      <footer><a href="https://example.com/ad">Ad</a></footer>
    </body></html>
    """


def _detail_html(
    title: str = "YASMIN LEVI",
    start: str = "06. 11. 2026.",
    end: str = "06. 11. 2026.",
) -> str:
    body = (
        "Ne propustite jedinstveno muzičko iskustvo u Beogradu. "
        "Zvanična stranica događaja sadrži detalje o programu, mestu održavanja i terminu. "
        "Posetiocima su dostupne informacije potrebne za planiranje dolaska."
    )
    return f"""
    <html><body>
      <header><h1>Turistička organizacija Beograda</h1></header>
      <h1>{title}</h1>
      <div>{start}</div>
      <h2>{title} | Informacije</h2>
      <ul>
        <li>Datum početka: {start}</li>
        <li>Datum završetka: {end}</li>
        <li>Mesto održavanja: Beogradska arena</li>
      </ul>
      <p>{body}</p>
      <h1>Ne propustite</h1>
      <a href="/sr/events/yasmin-levi">{title}</a>
      <p>Recommendation noise that must not enter evidence.</p>
      <footer>SEO footer noise</footer>
    </body></html>
    """


def test_event_dates_remain_evidence_without_publication_timestamp() -> None:
    item = parse_tob_event(
        _detail_html(),
        "https://www.tob.rs/sr/events/yasmin-levi",
        _source(),
    )

    assert item.title == "YASMIN LEVI"
    assert item.published_at is None
    assert "Datum početka: 06. 11. 2026." in item.body
    assert "Datum završetka: 06. 11. 2026." in item.body
    assert "Mesto održavanja: Beogradska arena" in item.body
    assert "Recommendation noise" not in item.body
    assert "SEO footer noise" not in item.body


def test_displayed_date_must_match_structured_start_date() -> None:
    html = _detail_html().replace(
        "<div>06. 11. 2026.</div>",
        "<div>07. 11. 2026.</div>",
        1,
    )
    with pytest.raises(ValueError, match="displayed event date does not match start date"):
        parse_tob_event(
            html,
            "https://www.tob.rs/sr/events/yasmin-levi",
            _source(),
        )


def test_end_before_start_fails_closed() -> None:
    with pytest.raises(ValueError, match="end precedes start"):
        parse_tob_event(
            _detail_html(start="06. 11. 2026.", end="05. 11. 2026."),
            "https://www.tob.rs/sr/events/yasmin-levi",
            _source(),
        )


@pytest.mark.asyncio
async def test_review_only_policy_cannot_be_relaxed() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as client:
        with pytest.raises(ValueError, match="review-only"):
            TobEventsCollector(unsafe, client)


@pytest.mark.asyncio
async def test_factory_collects_only_listing_events_and_records_partial_failure() -> None:
    source = _source()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/sr/events":
            return httpx.Response(200, text=_list_html())
        if path == "/sr/events/yasmin-levi":
            return httpx.Response(200, text=_detail_html())
        if path == "/sr/events/andre-riju":
            return httpx.Response(200, text="<html><h1>Andre Riju</h1></html>")
        raise AssertionError(path)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, TobEventsCollector)
        items = await collector.collect()

    assert [item.title for item in items] == ["YASMIN LEVI"]
    assert items[0].published_at is None
    assert len(collector.item_failures) == 1
    assert collector.item_failures[0]["url"].endswith("/sr/events/andre-riju")


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/sr/events"})

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="unapproved TOB URL"):
            await TobEventsCollector(source, client).collect()

    assert len(requested) == 1


FIXTURES = Path("tests/fixtures")


@pytest.mark.asyncio
async def test_live_20261121_official_empty_listing_is_empty_not_error() -> None:
    # Regression: source-health 2026-11-21 failed with "TOB event detail links
    # missing" while the official page rendered no events at all.
    empty = (FIXTURES / "tob_events_empty_20261121.html").read_text(encoding="utf-8")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=empty)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = TobEventsCollector(_source(), client)
        assert await collector.collect() == []
    assert collector.item_failures == []
    assert requested == ["https://www.tob.rs/sr/events"]


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        # Unknown cards inside the holder are a layout change, not an empty listing.
        ('<div id="sort-holder">', '<div id="sort-holder"><div class="card">Novo</div>'),
        # Featured events present while the main list has no recognised links.
        ("<h1>Ne propustite</h1>\n    <ul>", "<h1>Ne propustite</h1>\n    <ul><li>X</li>"),
        # Holder removed entirely: the structure is no longer recognised.
        ('<div id="sort-holder">', '<div id="other-holder">'),
    ],
)
@pytest.mark.asyncio
async def test_unrecognised_listing_still_fails_closed(needle: str, replacement: str) -> None:
    empty = (FIXTURES / "tob_events_empty_20261121.html").read_text(encoding="utf-8")
    assert needle in empty
    changed = empty.replace(needle, replacement, 1)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=changed))
    ) as client:
        collector = TobEventsCollector(_source(), client)
        with pytest.raises(ValueError, match="TOB event detail links missing"):
            await collector.collect()
