from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.euprava_service import (
    EupravaServiceCollector,
    parse_euprava_service,
)
from relocant_media.collectors.factory import build_collector
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/euprava_foreigner_registration.yaml")


def _source():
    return load_source_file(SOURCE_PATH)


def _html(*, description_suffix: str = "", noise: str = "Барајево") -> str:
    description = (
        "Електронска услуга је намењена физичким лицима која врше пријаву боравишта "
        "странца на територији Републике Србије. Подносилац користи званични електронски "
        "образац, а потврда се доставља електронским путем. "
        * 4
    ) + description_suffix
    return f"""
    <html><body>
      <nav>Грађани Привреда Држава {noise}</nav>
      <h1>Пријава боравишта странца</h1>
      <button>Читај ми</button>
      <h2>Предуслови</h2>
      <p>Грађанин је пријављен на портал еУправа квалификованим електронским сертификатом.</p>
      <h2>Опис</h2>
      <p>{description}</p>
      <h2>Ограничења</h2>
      <p>Услуга има ограничења у вези са адресом и подацима о непокретности.</p>
      <h2>Корисни линкови</h2>
      <p>Званична Интернет презентација Министарства унутрашњих послова.</p>
      <h2>Правни основ</h2>
      <p>Закон о странцима и одговарајући правилник о пријави боравишта странца.</p>
      <div>Промените</div>
      <div>Избор локације</div>
      <select><option>{noise}</option><option>Град Београд</option></select>
      <footer>Портал еУправа Републике Србије {noise}</footer>
    </body></html>
    """


def test_scoped_service_ignores_navigation_and_location_noise() -> None:
    source = _source()
    first = parse_euprava_service(_html(noise="Барајево"), source)
    second = parse_euprava_service(_html(noise="Нови Београд"), source)

    assert first.external_id == second.external_id
    assert first.body == second.body
    assert "Избор локације" not in first.body
    assert "Барајево" not in first.body
    assert first.published_at is None
    assert first.url == str(source.url)
    assert first.source_html is not None


def test_service_content_change_changes_revision_identity() -> None:
    source = _source()
    before = parse_euprava_service(_html(), source)
    after = parse_euprava_service(
        _html(description_suffix=" Додат је нови званични услов услуге."),
        source,
    )

    assert before.external_id != after.external_id
    assert before.canonical_url == after.canonical_url == str(source.url)


def test_missing_required_section_fails_closed() -> None:
    html = _html().replace("<h2>Правни основ</h2>", "<h2>Друга секција</h2>")
    with pytest.raises(ValueError, match="required section missing: Правни основ"):
        parse_euprava_service(html, _source())


def test_high_risk_policy_cannot_be_relaxed() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    with pytest.raises(ValueError, match="high-risk and review-only"):
        parse_euprava_service(_html(), unsafe)


@pytest.mark.asyncio
async def test_factory_collects_scoped_snapshot_without_publication_date() -> None:
    source = _source()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "euprava.gov.rs"
        assert request.url.path == "/usluge/00008"
        return httpx.Response(200, text=_html())

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, EupravaServiceCollector)
        items = await collector.collect()

    assert len(items) == 1
    assert items[0].published_at is None
    assert items[0].title == "Пријава боравишта странца"


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/usluge/00008"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="unapproved eUprava service URL"):
            await EupravaServiceCollector(source, client).collect()

    assert len(requested) == 1
