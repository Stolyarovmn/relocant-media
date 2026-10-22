from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.bgprevoz_news import (
    BELGRADE_TZ,
    BgprevozNewsCollector,
    parse_bgprevoz_news_article,
    parse_bgprevoz_news_list,
)
from relocant_media.collectors.factory import build_collector
from relocant_media.sources.registry import load_source_file


def _source():
    return load_source_file(Path("cities/belgrade/sources/bgprevoz_news.yaml"))


def _card(
    slug: str = "promena-trase-linije-br-708",
    *,
    title: str = "Промена трасе линије бр. 708",
    date: str = "30-09-2026",
) -> str:
    return f"""
    <article class="news-card">
      <h2><a href="/vesti/{slug}">{title}</a></h2>
      <h3>{date}</h3>
      <p>Кратак увод који служи само за откривање вести.</p>
      <a href="/index.php/vesti/{slug}">Опширније</a>
    </article>
    """


def _detail(title: str = "Промена трасе линије бр. 708") -> str:
    body = (
        "Од четвртка се мења траса линије јавног превоза на захтев градске општине. "
        "Линија задржава главни део трасе, а затим саобраћа измењеним улицама до "
        "одредишта. На овај начин обезбеђује се боља повезаност становника јавним превозом."
    )
    return f"""
    <html><body>
      <nav>Почетна Вести Контакт</nav>
      <h1>{title}</h1>
      <p>{body}</p>
      <h4>Секретаријат за јавни превоз</h4>
      <footer>Градска управа града Београда</footer>
    </body></html>
    """


def test_list_binds_title_url_and_card_date() -> None:
    candidates = parse_bgprevoz_news_list(_card(), _source())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.slug == "promena-trase-linije-br-708"
    assert candidate.url == "https://bgprevoz.rs/vesti/promena-trase-linije-br-708"
    assert candidate.title == "Промена трасе линије бр. 708"
    assert candidate.published_at == datetime(2026, 9, 30, tzinfo=BELGRADE_TZ)


def test_missing_card_date_cannot_borrow_date_from_neighboring_card() -> None:
    undated = """
    <article class="news-card">
      <h2><a href="/vesti/bez-datuma">Вест без датума</a></h2>
      <p>Без датума.</p>
    </article>
    """
    candidates = parse_bgprevoz_news_list(
        f"<main>{_card()}{undated}</main>",
        _source(),
    )
    assert [item.slug for item in candidates] == ["promena-trase-linije-br-708"]


def test_future_card_date_is_not_accepted() -> None:
    future = (datetime.now(BELGRADE_TZ) + timedelta(days=2)).strftime("%d-%m-%Y")
    candidates = parse_bgprevoz_news_list(
        _card() + _card("future", title="Будућа вест", date=future),
        _source(),
    )
    assert [item.slug for item in candidates] == ["promena-trase-linije-br-708"]


def test_detail_uses_exact_card_date_and_full_body() -> None:
    published = datetime(2026, 9, 30, tzinfo=BELGRADE_TZ)
    item = parse_bgprevoz_news_article(
        _detail(),
        _source(),
        page_url="https://www.bgprevoz.rs/index.php/vesti/promena-trase-linije-br-708",
        published_at=published,
        expected_title="Промена трасе линије бр. 708",
    )
    assert item.external_id == "bgprevoz-news-promena-trase-linije-br-708"
    assert item.published_at == published
    assert item.canonical_url == "https://bgprevoz.rs/vesti/promena-trase-linije-br-708"
    assert "боља повезаност" in item.body
    assert "Секретаријат за јавни превоз" not in item.body
    assert item.source_html is not None


def test_detail_title_must_match_dated_card() -> None:
    with pytest.raises(ValueError, match="does not match dated index card"):
        parse_bgprevoz_news_article(
            _detail(title="Други наслов"),
            _source(),
            page_url="https://bgprevoz.rs/vesti/promena-trase-linije-br-708",
            published_at=datetime(2026, 9, 30, tzinfo=BELGRADE_TZ),
            expected_title="Промена трасе линије бр. 708",
        )


@pytest.mark.asyncio
async def test_collect_follows_only_explicit_approved_www_redirect() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/vesti" and request.url.host == "bgprevoz.rs":
            return httpx.Response(200, text=_card())
        if request.url.host == "bgprevoz.rs":
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://www.bgprevoz.rs/index.php/vesti/"
                        "promena-trase-linije-br-708"
                    )
                },
            )
        if request.url.host == "www.bgprevoz.rs":
            return httpx.Response(200, text=_detail())
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, BgprevozNewsCollector)
        items = await collector.collect()

    assert len(items) == 1
    assert items[0].published_at == datetime(2026, 9, 30, tzinfo=BELGRADE_TZ)
    assert len(requested) == 3


@pytest.mark.asyncio
async def test_external_redirect_is_rejected_before_second_request() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/vesti":
            return httpx.Response(200, text=_card())
        return httpx.Response(302, headers={"Location": "https://example.com/news"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = BgprevozNewsCollector(source, client)
        with pytest.raises(ValueError, match="all dated Bgprevoz news"):
            await collector.collect()
    assert len(requested) == 2
    assert collector.item_failures[0]["error_message"] == "unapproved Bgprevoz news URL"


@pytest.mark.asyncio
async def test_collect_url_requires_same_article_on_dated_index() -> None:
    source = _source()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/vesti":
            return httpx.Response(200, text=_card())
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = BgprevozNewsCollector(source, client)
        with pytest.raises(ValueError, match="not on the dated index page"):
            await collector.collect_url("https://bgprevoz.rs/vesti/stara-vest")
