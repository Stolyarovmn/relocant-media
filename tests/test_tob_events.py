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
