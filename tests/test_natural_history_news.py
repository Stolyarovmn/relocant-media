from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.natural_history_news import (
    NaturalHistoryNewsCollector,
    parse_natural_history_article,
)
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/venue_natural_history.yaml")
BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
ARTICLE_PATH = "/o-korenima-srpske-geologije/"
ARTICLE_URL = f"https://nhmbeo.rs{ARTICLE_PATH}"


def _source():
    return load_source_file(SOURCE_PATH)


def _listing(*, first_date: str = "12.10.2026.", second_date: str = "07.10.2026.") -> str:
    return f"""
    <html><body>
      <h2>Вести</h2>
      <h3><a href="{ARTICLE_PATH}">О КОРЕНИМА СРПСКЕ ГЕОЛОГИЈЕ</a></h3>
      <p>Природњачки музеј позива публику на свечано отварање изложбе.</p>
      <a href="{ARTICLE_PATH}">Прочитај више »</a>
      <div>{first_date}</div>
      <h3><a href="/noc-slepih-miseva/">30. МЕЂУНАРОДНА НОЋ СЛЕПИХ МИШЕВА</a></h3>
      <p>Традиционална манифестација Природњачког музеја.</p>
      <a href="/noc-slepih-miseva/">Прочитај више »</a>
      <div>{second_date}</div>
      <h3><a href="/kontakt/">Контакт</a></h3>
    </body></html>
    """


def _detail(title: str = "О КОРЕНИМА СРПСКЕ ГЕОЛОГИЈЕ") -> str:
    body = (
        "Природњачки музеј позива публику на званични програм у Галерији музеја на "
        "Калемегдану, са јасно наведеним садржајем, условима посете и трајањем изложбе. "
        * 4
    )
    return f"""
    <html><body>
      <h2>{title}</h2>
      <p>{body}</p>
      <p>Улаз је слободан. Добро дошли!</p>
      <h3>Newsletter</h3>
      <p>Овај текст не сме да уђе у тело вести.</p>
    </body></html>
    """


def test_detail_keeps_explicit_listing_publication_date() -> None:
    published = datetime(2026, 10, 12, tzinfo=BELGRADE_TZ)
    item = parse_natural_history_article(
        _detail(),
        ARTICLE_URL,
        "О КОРЕНИМА СРПСКЕ ГЕОЛОГИЈЕ",
        published,
        _source(),
    )

    assert item.published_at == published
    assert item.title == "О КОРЕНИМА СРПСКЕ ГЕОЛОГИЈЕ"
    assert "Newsletter" not in item.body
    assert "Овај текст не сме" not in item.body
    assert item.source_html is not None
    assert item.external_id == item.canonical_url == item.url


@pytest.mark.asyncio
async def test_factory_binds_each_card_to_its_own_date_and_full_article() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/vesti/":
            return httpx.Response(200, text=_listing())
        if request.url.path == ARTICLE_PATH:
            return httpx.Response(200, text=_detail())
        if request.url.path == "/noc-slepih-miseva/":
            return httpx.Response(
                200,
                text=_detail("30. МЕЂУНАРОДНА НОЋ СЛЕПИХ МИШЕВА"),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, NaturalHistoryNewsCollector)
        items = await collector.collect()

    assert len(items) == 2
    assert [item.published_at for item in items] == [
        datetime(2026, 10, 12, tzinfo=BELGRADE_TZ),
        datetime(2026, 10, 7, tzinfo=BELGRADE_TZ),
    ]
    assert all("kontakt" not in value for value in requested)


@pytest.mark.asyncio
async def test_missing_or_ambiguous_card_date_fails_closed() -> None:
    source = _source()

    def missing_date(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/vesti/"
        return httpx.Response(200, text=_listing(first_date="nije datum"))

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(missing_date),
    ) as client:
        with pytest.raises(ValueError, match="card date missing or ambiguous"):
            await NaturalHistoryNewsCollector(source, client).collect()


def test_publication_dates_must_be_descending() -> None:
    source = _source()

    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/vesti/"
            return httpx.Response(
                200,
                text=_listing(first_date="07.10.2026.", second_date="12.10.2026."),
            )

        async with httpx.AsyncClient(
            trust_env=False,
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ValueError, match="publication dates are not descending"):
                await NaturalHistoryNewsCollector(source, client).collect()

    import asyncio

    asyncio.run(run())


@pytest.mark.asyncio
async def test_auto_publish_and_cross_host_redirect_are_rejected() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ValueError, match="must stay review-only"):
            NaturalHistoryNewsCollector(unsafe, client)

    requested: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/vesti/"})

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(redirect),
    ) as client:
        with pytest.raises(ValueError, match="unapproved Natural History Museum URL"):
            await NaturalHistoryNewsCollector(source, client).collect()

    assert len(requested) == 1
