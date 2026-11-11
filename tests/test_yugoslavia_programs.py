from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.yugoslavia_programs import (
    YugoslaviaProgramsCollector,
    parse_yugoslavia_program,
)
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/venue_yugoslavia.yaml")
DETAIL_PATH = "/rs/program/besplatne-projekcije-tv-serije-banjica/"
DETAIL_URL = f"https://muzej-jugoslavije.org{DETAIL_PATH}"
LIVE_FIXTURE = Path("tests/fixtures/yugoslavia_program_20261111.html")
LIVE_PATH = "/rs/program/porodicni-dan-idealni-neboder-2/"


def _source():
    return load_source_file(SOURCE_PATH)


def _listing() -> str:
    return f"""
    <html><body>
      <h1>ИЗЛОЖБЕ И ПРОГРАМИ</h1>
      <a href="{DETAIL_PATH}">
        Програм Бесплатне пројекције ТВ серије „Бањица” 20.10.2026 - 24.10.2026
      </a>
      <a href="/rs/izlozba/citaj-i-deli-dalje/">
        Изложба Читај и дели даље 15.09.2026 - 31.03.2027
      </a>
      <a href="/rs/lista-programa/">Листа програма</a>
    </body></html>
    """


def _detail(*, day: str = "Дан: 20.10-24.10.2026.", include_time: bool = True) -> str:
    body = (
        "У склопу пратећих програма изложбе Музеј Југославије организује званични "
        "јавни програм са јасно наведеним терминима, местом одржавања и условима посете. "
        * 4
    )
    time = "<li>Време: 17:00</li>" if include_time else ""
    return f"""
    <html><body>
      <a href="/rs/lista-programa/">Листа програма</a>
      <h1>Бесплатне пројекције ТВ серије „Бањица”</h1>
      <p>{body}</p>
      <p>Улаз на све пратеће програме је слободан.</p>
      <a href="/rs/lokacija/muzej-25-maj-bioskopska-sala/">
        Музеј 25. мај - Биоскопска сала
      </a>
      <ul>
        <li>{day}</li>
        {time}
      </ul>
      <footer>Музеј Југославије</footer>
    </body></html>
    """


def test_detail_extracts_full_program_without_inventing_publication_date() -> None:
    item = parse_yugoslavia_program(_detail(), DETAIL_URL, _source())

    assert item.title == "Бесплатне пројекције ТВ серије „Бањица”"
    assert item.published_at is None
    assert "Место: Музеј 25. мај - Биоскопска сала" in item.body
    assert "Дан: 20.10-24.10.2026." in item.body
    assert "Време: 17:00" in item.body
    assert item.source_html is not None
    assert item.external_id == item.canonical_url == item.url


def test_time_is_optional_but_day_range_is_required() -> None:
    source = _source()
    item = parse_yugoslavia_program(
        _detail(include_time=False),
        "https://muzej-jugoslavije.org/rs/program/arhitektura-modernizma/",
        source,
    )
    assert "Време:" not in item.body

    with pytest.raises(ValueError, match="exactly one day range"):
        parse_yugoslavia_program(
            _detail().replace("<li>Дан: 20.10-24.10.2026.</li>", ""),
            "https://muzej-jugoslavije.org/rs/program/test-program/",
            source,
        )


def test_invalid_or_reversed_day_range_fails_closed() -> None:
    with pytest.raises(ValueError, match="end precedes start"):
        parse_yugoslavia_program(
            _detail(day="Дан: 24.10-20.10.2026."),
            "https://muzej-jugoslavije.org/rs/program/test-program/",
            _source(),
        )


def test_captured_program_preserves_missing_location_without_inferring_genex_or_footer() -> None:
    html = LIVE_FIXTURE.read_text(encoding="utf-8")
    html += "<footer>Музеј Југославије, Михаила Мике Јанковића 6, Београд</footer>"
    item = parse_yugoslavia_program(
        html, f"https://muzej-jugoslavije.org{LIVE_PATH}", _source(),
    )
    assert item.title == "Породични дан: Идеални небодер"
    assert "Кула „Генекс”" in item.body
    assert "Место:" not in item.body
    assert "Михаила Мике Јанковића" not in item.body
    assert "Дан: 08.11-08.11.2026." in item.body
    assert "Време: 12:00" in item.body
    assert item.body.count("Придружите нам се") == 1
    assert item.published_at is None
    assert not _source().policy.auto_publish


@pytest.mark.parametrize("day", ["", "Дан: 99.11-99.11.2026."])
def test_captured_program_still_requires_valid_day(day: str) -> None:
    html = LIVE_FIXTURE.read_text(encoding="utf-8")
    if not day:
        html = html.replace("Дан:", "")
    else:
        html = html.replace("08.11-08.11.2026.", "99.11-99.11.2026.")
    with pytest.raises(ValueError, match="day range"):
        parse_yugoslavia_program(html, f"https://muzej-jugoslavije.org{LIVE_PATH}", _source())


def test_registration_and_foreign_location_links_are_not_venue_evidence() -> None:
    html = _detail().replace(
        '/rs/lokacija/muzej-25-maj-bioskopska-sala/',
        'https://foreign.example/rs/lokacija/muzej-25-maj-bioskopska-sala/',
    )
    item = parse_yugoslavia_program(html, DETAIL_URL, _source())
    assert "Место:" not in item.body


def test_current_calendar_program_with_only_list_anchor_keeps_partial_evidence() -> None:
    html = Path("tests/fixtures/yugoslavia_current_program_20261111.html").read_text(
        encoding="utf-8",
    )
    item = parse_yugoslavia_program(
        html,
        "https://muzej-jugoslavije.org/rs/program/"
        "razgovori-o-jugoslaviji-jugoslovenski-avio-saobracaj/",
        _source(),
    )
    assert item.title == "Разговори о Југославији: Југословенски авио-саобраћај"
    assert "Место:" not in item.body
    assert "Листа програма" not in item.body
    assert any(line.startswith("Дан:") for line in item.body.splitlines())
    assert item.published_at is None


def test_captured_program_collects_partial_evidence_with_explicit_audit() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/rs/kalendar/":
            return httpx.Response(200, text=_listing().replace(DETAIL_PATH, LIVE_PATH))
        if request.url.path == LIVE_PATH:
            return httpx.Response(200, text=LIVE_FIXTURE.read_text(encoding="utf-8"))
        raise AssertionError(request.url)

    async def offline_collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            collector = YugoslaviaProgramsCollector(_source(), client)
            items = await collector.collect()
            return items, collector.item_failures

    with pytest.raises(StopIteration) as completed:
        offline_collect().send(None)
    items, failures = completed.value.value
    assert len(items) == 1
    assert len(failures) == 1
    assert failures[0]["error_type"] == "IncompleteProgramEvidence"
    assert failures[0]["url"] == items[0].url
    assert "location absent" in failures[0]["error_message"]
    assert items[0].published_at is None
    assert "Место:" not in items[0].body
    assert requested == ["/rs/kalendar/", LIVE_PATH]


@pytest.mark.asyncio
async def test_factory_uses_calendar_and_ignores_exhibition_links() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/rs/kalendar/":
            return httpx.Response(200, text=_listing())
        if request.url.path == DETAIL_PATH:
            return httpx.Response(200, text=_detail())
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, YugoslaviaProgramsCollector)
        items = await collector.collect()

    assert len(items) == 1
    assert items[0].published_at is None
    assert not any("izlozba" in value for value in requested)


@pytest.mark.asyncio
async def test_auto_publish_cannot_be_enabled_without_publication_dates() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ValueError, match="must stay review-only"):
            YugoslaviaProgramsCollector(unsafe, client)


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/rs/kalendar/"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="unapproved Museum of Yugoslavia URL"):
            await YugoslaviaProgramsCollector(source, client).collect()

    assert len(requested) == 1
