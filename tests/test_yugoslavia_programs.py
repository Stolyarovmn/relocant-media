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


def _source():
    return load_source_file(SOURCE_PATH)


def _listing() -> str:
    return """
    <html><body>
      <h1>ИЗЛОЖБЕ И ПРОГРАМИ</h1>
      <a href="/rs/program/besplatne-projekcije-tv-serije-banjica/">
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
      <a href="/rs/lokacija/muzej-25-maj-bioskopska-sala/">Музеј 25. мај - Биоскопска сала</a>
      <ul>
        <li>{day}</li>
        {time}
      </ul>
      <footer>Музеј Југославије</footer>
    </body></html>
    """


def test_detail_extracts_full_program_without_inventing_publication_date() -> None:
    source = _source()
    item = parse_yugoslavia_program(
        _detail(),
        "https://muzej-jugoslavije.org/rs/program/besplatne-projekcije-tv-serije-banjica/",
        source,
    )

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
        "https://muzej-jugoslavije.org/rs/program/arhitektura-jugoslovenskog-modernizma-beograd-7-1/",
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
    source = _source()
    with pytest.raises(ValueError, match="end precedes start"):
        parse_yugoslavia_program(
            _detail(day="Дан: 24.10-20.10.2026."),
            "https://muzej-jugoslavije.org/rs/program/test-program/",
            source,
        )


@pytest.mark.asyncio
async def test_factory_uses_calendar_and_ignores_exhibition_links() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/rs/kalendar/":
            return httpx.Response(200, text=_listing())
        if request.url.path == "/rs/program/besplatne-projekcije-tv-serije-banjica/":
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


def test_auto_publish_cannot_be_enabled_without_publication_dates() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    with httpx.Client() as _client:
        pass
    with pytest.raises(ValueError, match="must stay review-only"):
        YugoslaviaProgramsCollector(unsafe, httpx.AsyncClient())


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
