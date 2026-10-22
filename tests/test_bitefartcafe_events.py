from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.bitefartcafe_events import (
    BitefArtCafeEventsCollector,
    _concert_links,
    parse_bitefartcafe_event,
)
from relocant_media.collectors.factory import build_collector
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/venue_bitefartcafe.yaml")
LIST_PATH = "/program/koncerti/"
FIRST_PATH = "/dogadjaji/psihomodo-pop/"
SECOND_PATH = "/dogadjaji/kovacs-musicology-barcaffe-sessions/"
FIRST_URL = f"https://bitefartcafe.rs{FIRST_PATH}"


def _source():
    return load_source_file(SOURCE_PATH)


def _listing() -> str:
    return f"""
    <html><body>
      <div>Koncerti</div>
      <a href="{FIRST_PATH}">
        <h2>PSIHOMODO POP</h2><span>30.10.2026.</span><span>22h</span>
      </a>
      <a href="{SECOND_PATH}">
        <h2>KOVACS - Musicology Barcaffè sessions</h2>
        <span>04.11.2026.</span><span>21h</span>
      </a>
      <a href="/kontakt/">Kontakt</a>
    </body></html>
    """


def _detail(title: str = "PSIHOMODO POP", date: str = "30.10.2026.") -> str:
    body = (
        "BitefArtCafe objavljuje zvaničan koncertni program sa uslovima ulaska, "
        "terminom, informacijama o ulaznicama i detaljima događaja za publiku. "
        * 3
    )
    return f"""
    <html>
      <head><title>{title} - BitefArtCafe</title></head>
      <body>
        <div>{date}</div><div>22h</div><div>BITEFARTCAFE</div>
        <h2>{title}</h2><h2>{title}</h2>
        <p>{body}</p>
        <p>Na koncertima postoji starosno ograničenje 18+.</p>
        <h2>Online Rezervacije</h2>
        <p>Ovaj tekst ne sme da uđe u telo dokaza.</p>
      </body>
    </html>
    """


def test_listing_uses_only_canonical_event_detail_links() -> None:
    links = _concert_links(_listing(), _source())

    assert links == [
        "https://bitefartcafe.rs/dogadjaji/psihomodo-pop/",
        "https://bitefartcafe.rs/dogadjaji/kovacs-musicology-barcaffe-sessions/",
    ]


def test_detail_keeps_event_date_but_does_not_invent_publication_date() -> None:
    item = parse_bitefartcafe_event(_detail(), FIRST_URL, _source())

    assert item.title == "PSIHOMODO POP"
    assert item.published_at is None
    assert item.body.startswith("Datum događaja: 30.10.2026.\n")
    assert "starosno ograničenje 18+" in item.body
    assert "Ovaj tekst ne sme" not in item.body
    assert item.external_id == item.canonical_url == item.url == FIRST_URL
    assert item.source_html == _detail()


@pytest.mark.asyncio
async def test_factory_fetches_full_official_details() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == LIST_PATH:
            return httpx.Response(200, text=_listing())
        if request.url.path == FIRST_PATH:
            return httpx.Response(200, text=_detail())
        if request.url.path == SECOND_PATH:
            return httpx.Response(
                200,
                text=_detail(
                    "KOVACS - Musicology Barcaffè sessions",
                    "04.11.2026.",
                ),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, BitefArtCafeEventsCollector)
        items = await collector.collect()

    assert len(items) == 2
    assert requested == [LIST_PATH, FIRST_PATH, SECOND_PATH]
    assert all(item.published_at is None for item in items)


@pytest.mark.asyncio
async def test_auto_publish_and_cross_host_redirect_are_rejected() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ValueError, match="must stay review-only"):
            BitefArtCafeEventsCollector(unsafe, client)

    requested: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/program/koncerti/"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(redirect),
    ) as client:
        with pytest.raises(ValueError, match="unapproved BitefArtCafe URL"):
            await BitefArtCafeEventsCollector(source, client).collect()

    assert len(requested) == 1


def test_source_is_structured_review_only_without_rss() -> None:
    source = _source()

    assert source.extraction.strategy == "bitefartcafe_events"
    assert source.extraction.feed_url is None
    assert source.extraction.approved_hosts == (
        "bitefartcafe.rs",
        "www.bitefartcafe.rs",
    )
    assert source.policy.auto_publish is False
    assert str(source.url) == "https://bitefartcafe.rs/program/koncerti/"
