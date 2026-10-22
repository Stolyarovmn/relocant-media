from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.beograd_article import parse_beograd_article
from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.health_duty import (
    BELGRADE_TZ,
    HealthDutyCollector,
    parse_health_duty_list,
)
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/health_duty.yaml")


def _source():
    return load_source_file(SOURCE_PATH)


def _date_text(value: datetime) -> str:
    return value.strftime("%d.%m.%Y.")


def _card(
    article_id: str = "116316",
    *,
    date: str = "21.10.2026.",
) -> str:
    return (
        '<a href="/lat/servisne-informacije/zdravstvo/'
        f'a{article_id}/Raspored-dezurstava-za-prijem-hitnih-slucajeva.html">'
        f"ZDRAVSTVO: Dežurne zdravstvene ustanove za {date} godine "
        f"Zdravstvo {date}</a>"
    )


def _detail(date: str, *, body: str | None = None) -> str:
    body = body or (
        "U navedenom danu dežurne su zdravstvene ustanove sa zvanično objavljenog spiska. "
        "Kliničko-bolnički centar prima hitne slučajeve, a stranica navodi adresu, telefone "
        "i oblasti za koje je ustanova dežurna. Gradska hitna pomoć radi neprekidno."
    )
    return f"""
    <main>
      <h1>ZDRAVSTVO: Dežurne zdravstvene ustanove za {date} godine</h1>
      <div class="story__time">{date}</div>
      <p>{body}</p>
      <footer><p>Politika privatnosti i kontakt.</p></footer>
    </main>
    """


def test_health_article_path_is_supported_by_shared_full_article_parser() -> None:
    url = (
        "https://www.beograd.rs/lat/servisne-informacije/zdravstvo/"
        "a116316/Raspored-dezurstava-za-prijem-hitnih-slucajeva.html"
    )
    item = parse_beograd_article(_detail("21.10.2026."), _source(), url)
    assert item.external_id == "116316"
    assert item.published_at == datetime(2026, 10, 21, tzinfo=BELGRADE_TZ)
    assert "Kliničko-bolnički" in item.body
    assert "Politika privatnosti" not in item.body


def test_index_binds_exact_health_article_id_and_date() -> None:
    candidates = parse_health_duty_list(_card(), _source())
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.article_id == "116316"
    assert candidate.published_at == datetime(2026, 10, 21, tzinfo=BELGRADE_TZ)
    assert candidate.url.startswith(
        "https://www.beograd.rs/lat/servisne-informacije/zdravstvo/a116316/"
    )


def test_source_policy_remains_high_risk_and_review_only() -> None:
    source = _source()
    assert source.ownership == "official"
    assert source.trust == 1.0
    assert source.extraction.strategy == "health_duty"
    assert source.extraction.status == "active"
    assert source.extraction.approved_hosts == ("beograd.rs",)
    assert source.policy.high_risk is True
    assert source.policy.auto_publish is False

    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None))
    try:
        with pytest.raises(ValueError, match="high-risk and review-only"):
            HealthDutyCollector(unsafe, async_client)
    finally:
        import asyncio

        asyncio.run(async_client.aclose())


def test_future_index_date_is_not_accepted_as_current_duty() -> None:
    future = datetime.now(BELGRADE_TZ) + timedelta(days=2)
    current = datetime.now(BELGRADE_TZ) - timedelta(days=1)
    html = _card("116316", date=_date_text(current)) + _card(
        "116317",
        date=_date_text(future),
    )
    candidates = parse_health_duty_list(html, _source())
    assert [item.article_id for item in candidates] == ["116316"]


@pytest.mark.asyncio
async def test_collect_requires_index_and_detail_dates_to_match() -> None:
    source = _source()
    current = datetime.now(BELGRADE_TZ) - timedelta(days=1)
    wrong = current - timedelta(days=1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/zdravstvo"):
            return httpx.Response(200, text=_card(date=_date_text(current)))
        return httpx.Response(200, text=_detail(_date_text(wrong)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = HealthDutyCollector(source, client)
        with pytest.raises(ValueError, match="all recent health duty"):
            await collector.collect()
    assert collector.item_failures[0]["error_message"] == (
        "health duty detail date does not match dated index link"
    )


@pytest.mark.asyncio
async def test_collect_returns_full_recent_evidence_and_provenance() -> None:
    source = _source()
    current = datetime.now(BELGRADE_TZ) - timedelta(days=1)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.endswith("/zdravstvo"):
            return httpx.Response(200, text=_card(date=_date_text(current)))
        if request.url.host == "www.beograd.rs":
            return httpx.Response(
                302,
                headers={"Location": f"https://beograd.rs{request.url.path}"},
            )
        return httpx.Response(200, text=_detail(_date_text(current)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, HealthDutyCollector)
        items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "116316"
    assert item.published_at == datetime(
        current.year,
        current.month,
        current.day,
        tzinfo=BELGRADE_TZ,
    )
    assert item.source_html is not None
    assert item.approved_image_hosts == ("static.beograd.rs",)
    assert len(requested) == 3


@pytest.mark.asyncio
async def test_external_redirect_is_rejected_before_second_host_request() -> None:
    source = _source()
    current = datetime.now(BELGRADE_TZ) - timedelta(days=1)
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.endswith("/zdravstvo"):
            return httpx.Response(200, text=_card(date=_date_text(current)))
        return httpx.Response(302, headers={"Location": "https://example.com/health"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = HealthDutyCollector(source, client)
        with pytest.raises(ValueError, match="all recent health duty"):
            await collector.collect()
    assert len(requested) == 2
    assert collector.item_failures[0]["error_message"] == "unapproved health source URL"


@pytest.mark.asyncio
async def test_collect_url_requires_article_to_remain_on_dated_official_index() -> None:
    source = _source()
    current = datetime.now(BELGRADE_TZ) - timedelta(days=1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/zdravstvo"):
            return httpx.Response(200, text=_card("116316", date=_date_text(current)))
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = HealthDutyCollector(source, client)
        with pytest.raises(ValueError, match="not on the dated official index"):
            await collector.collect_url(
                "https://www.beograd.rs/lat/servisne-informacije/zdravstvo/"
                "a116999/stara-objava.html"
            )
