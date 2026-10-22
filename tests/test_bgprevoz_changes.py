from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.bgprevoz_changes import (
    BgprevozChangesCollector,
    parse_bgprevoz_change,
    parse_bgprevoz_change_links,
)
from relocant_media.collectors.factory import build_collector
from relocant_media.sources.registry import load_source_file


def _source():
    return load_source_file(Path("cities/belgrade/sources/bgprevoz_changes.yaml"))


def _detail(*, period: str = "23/10/2026 - 25/10/2026", body: str | None = None) -> str:
    body = body or (
        "Током извођења радова доћи ће до промене режима рада линија јавног превоза. "
        "Возила ће на измењеном делу трасе саобраћати алтернативним улицама и користити "
        "постојећа стајалишта дуж привремено измењене трасе."
    )
    return f"""
    <html><body>
      <nav>Почетна Линије Контакт</nav>
      <h1>Информација о промени режима рада линија јавног превоза</h1>
      <div class="routes">45</div><div class="routes">71</div>
      <div>{period}</div>
      <main><p>{body}</p></main>
      <div>Зона радова</div>
      <footer>Секретаријат за јавни превоз</footer>
    </body></html>
    """


def test_list_extracts_unique_canonical_notice_urls_only() -> None:
    source = _source()
    html = """
    <a href="/linije/aktuelne-izmene/1141">Опширније</a>
    <a href="https://bgprevoz.rs/index.php/linije/aktuelne-izmene/1141/601?smer=0">601</a>
    <a href="/linije/aktuelne-izmene/1139">Опширније</a>
    <a href="https://example.com/linije/aktuelne-izmene/9999">external</a>
    """
    assert parse_bgprevoz_change_links(html, source) == (
        "https://www.bgprevoz.rs/linije/aktuelne-izmene/1141",
        "https://www.bgprevoz.rs/linije/aktuelne-izmene/1139",
    )


def test_detail_preserves_effective_period_without_inventing_publication_date() -> None:
    source = _source()
    item = parse_bgprevoz_change(
        _detail(),
        source,
        page_url="https://www.bgprevoz.rs/linije/aktuelne-izmene/1141",
    )
    assert item.external_id == "bgprevoz-1141"
    assert item.url == "https://www.bgprevoz.rs/linije/aktuelne-izmene/1141"
    assert item.canonical_url == item.url
    assert item.published_at is None
    assert item.body.startswith("Период измене: 23.10.2026–25.10.2026.\n")
    assert "промене режима рада" in item.body
    assert item.source_html is not None


def test_multi_year_effective_period_is_valid_when_official_page_says_so() -> None:
    item = parse_bgprevoz_change(
        _detail(period="02/11/2024 - 02/09/2027"),
        _source(),
        page_url="https://www.bgprevoz.rs/linije/aktuelne-izmene/617",
    )
    assert item.external_id == "bgprevoz-617"
    assert item.body.startswith("Период измене: 02.11.2024–02.09.2027.\n")
    assert item.published_at is None


@pytest.mark.parametrize(
    ("period", "message"),
    [
        ("25/10/2026 - 23/10/2026", "period invalid"),
        ("not-a-period", "period missing"),
    ],
)
def test_invalid_or_missing_period_fails_closed(period: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_bgprevoz_change(
            _detail(period=period),
            _source(),
            page_url="https://www.bgprevoz.rs/linije/aktuelne-izmene/1141",
        )


def test_short_detail_is_not_accepted_as_full_evidence() -> None:
    with pytest.raises(ValueError, match="body shorter than 120"):
        parse_bgprevoz_change(
            _detail(body="Кратко обавештење."),
            _source(),
            page_url="https://www.bgprevoz.rs/linije/aktuelne-izmene/1141",
        )


@pytest.mark.asyncio
async def test_collector_fetches_list_then_exact_detail_and_keeps_unknown_publication_time() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/linije/aktuelne-izmene":
            return httpx.Response(
                200,
                text='<a href="/linije/aktuelne-izmene/1141">Опширније</a>',
            )
        if request.url.path == "/linije/aktuelne-izmene/1141":
            return httpx.Response(200, text=_detail())
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, BgprevozChangesCollector)
        items = await collector.collect()

    assert [item.external_id for item in items] == ["bgprevoz-1141"]
    assert items[0].published_at is None
    assert len(requested) == 2


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected_before_second_request() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/linije/aktuelne-izmene/1141"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="unapproved Bgprevoz URL"):
            await BgprevozChangesCollector(source, client).collect()

    assert len(requested) == 1


@pytest.mark.asyncio
async def test_one_bad_detail_is_bounded_partial_not_silent_loss() -> None:
    source = _source()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/linije/aktuelne-izmene":
            return httpx.Response(
                200,
                text=(
                    '<a href="/linije/aktuelne-izmene/1141">one</a>'
                    '<a href="/linije/aktuelne-izmene/1142">two</a>'
                ),
            )
        if request.url.path.endswith("/1141"):
            return httpx.Response(200, text=_detail())
        if request.url.path.endswith("/1142"):
            return httpx.Response(200, text=_detail(body="кратко"))
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = BgprevozChangesCollector(source, client)
        items = await collector.collect()

    assert [item.external_id for item in items] == ["bgprevoz-1141"]
    assert collector.item_failures == [
        {
            "url": "https://www.bgprevoz.rs/linije/aktuelne-izmene/1142",
            "error_type": "ValueError",
            "error_message": "Bgprevoz change body shorter than 120 characters",
        }
    ]
