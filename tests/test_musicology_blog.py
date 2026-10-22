from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.musicology_blog import (
    MusicologyBlogCollector,
    _blog_cards,
    parse_musicology_article,
)
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/venue_musicology.yaml")
LIST_PATH = "/blog/"
FIRST_PATH = "/2026/10/14/kovacs-musicology-barcaffe-sessions/"
SECOND_PATH = "/2026/09/10/nik-west-stize-u-beograd/"
FIRST_URL = f"https://musicology.rs{FIRST_PATH}"


def _source():
    return load_source_file(SOURCE_PATH)


def _listing() -> str:
    return f"""
    <html><body>
      <h1>Blog</h1>
      <h3><a href="{FIRST_PATH}">KOVACS stiže u Beograd</a></h3>
      <p>Najava koncerta 4. i 5. novembra.</p>
      <h3><a href="{SECOND_PATH}">Nik West stiže u Beograd</a></h3>
      <p>Najava septembarskog koncerta.</p>
      <h3><a href="/kontakt/">Kontakt</a></h3>
    </body></html>
    """


def _detail(
    title: str = "KOVACS stiže u Beograd",
    published: str = "2026-10-14T09:30:00+02:00",
) -> str:
    body = (
        "Musicology Barcaffè Sessions objavljuje zvaničnu najavu koncerta u Beogradu, "
        "sa terminom, lokacijom, informacijama o ulaznicama i uslovima ulaska. "
        * 3
    )
    return f"""
    <html>
      <head>
        <meta property="article:published_time" content="{published}">
        <script type="application/ld+json">
          {{"@type":"Article","datePublished":"{published}"}}
        </script>
      </head>
      <body>
        <h1>{title}</h1>
        <div>{body}</div>
        <div>Datum događaja: 04. novembar 2026.</div>
        <div>Na koncertima postoji starosno ograničenje 18+.</div>
        <h2>NEWSLETTER</h2>
        <p>Ovaj tekst ne sme da uđe u dokaz.</p>
      </body>
    </html>
    """


def test_listing_uses_only_dated_canonical_blog_links() -> None:
    cards = _blog_cards(_listing(), _source())

    assert cards == [
        (FIRST_URL, "KOVACS stiže u Beograd"),
        (
            "https://musicology.rs/2026/09/10/nik-west-stize-u-beograd/",
            "Nik West stiže u Beograd",
        ),
    ]


def test_detail_uses_explicit_publication_metadata_and_full_body() -> None:
    item = parse_musicology_article(
        _detail(), FIRST_URL, "KOVACS stiže u Beograd", _source()
    )

    assert item.published_at is not None
    assert item.published_at.isoformat() == "2026-10-14T09:30:00+02:00"
    assert "Datum događaja: 04. novembar 2026." in item.body
    assert "starosno ograničenje 18+" in item.body
    assert "Ovaj tekst ne sme" not in item.body
    assert item.external_id == item.canonical_url == item.url == FIRST_URL
    assert item.source_html == _detail()


def test_publication_metadata_must_match_permalink_date() -> None:
    with pytest.raises(ValueError, match="disagrees with permalink date"):
        parse_musicology_article(
            _detail(published="2026-10-13T23:30:00+02:00"),
            FIRST_URL,
            "KOVACS stiže u Beograd",
            _source(),
        )


@pytest.mark.asyncio
async def test_factory_fetches_official_blog_details() -> None:
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
                    "Nik West stiže u Beograd",
                    "2026-09-10T12:00:00+02:00",
                ),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, MusicologyBlogCollector)
        items = await collector.collect()

    assert len(items) == 2
    assert requested == [LIST_PATH, FIRST_PATH, SECOND_PATH]
    assert items[0].published_at is not None
    assert items[1].published_at is not None
    assert items[0].published_at > items[1].published_at


@pytest.mark.asyncio
async def test_auto_publish_and_cross_host_redirect_are_rejected() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ValueError, match="must stay review-only"):
            MusicologyBlogCollector(unsafe, client)

    requested: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/blog/"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(redirect),
    ) as client:
        with pytest.raises(ValueError, match="unapproved Musicology URL"):
            await MusicologyBlogCollector(source, client).collect()

    assert len(requested) == 1


def test_source_is_structured_review_only_without_rss() -> None:
    source = _source()

    assert source.extraction.strategy == "musicology_blog"
    assert source.extraction.feed_url is None
    assert source.extraction.approved_hosts == (
        "musicology.rs",
        "www.musicology.rs",
    )
    assert source.policy.auto_publish is False
    assert str(source.url) == "https://musicology.rs/blog/"
