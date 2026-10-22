from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.welcome_serbia_guidance import (
    GUIDANCE_TITLE,
    WelcomeSerbiaGuidanceCollector,
    parse_welcome_serbia_guidance,
)
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/welcome_serbia_temporary_residence.yaml")


def _source():
    return load_source_file(SOURCE_PATH)


def _html(*, suffix: str = "", noise: str = "footer-a") -> str:
    long_text = (
        "Zvanično uputstvo objašnjava uslove, postupak, dokumentaciju, rokove i prava stranca "
        "tokom postupka odobrenja privremenog boravka u Republici Srbiji. "
        * 24
    ) + suffix
    return f"""
    <html><body>
      <nav>Početna Pomoć Kontakt {noise}</nav>
      <h1>{GUIDANCE_TITLE}</h1>
      <h5>Mogućnost da regulišete boravak na period do 3 godine</h5>
      <h5>Šta je privremeni boravak?</h5>
      <p>{long_text}</p>
      <h5>Privremeni boravak i rad (jedinstvena dozvola)</h5>
      <p>Jedinstvena dozvola objedinjuje privremeni boravak i rad.</p>
      <h5>Kako do jedinstvene dozvole</h5>
      <p>Zahtev se podnosi elektronski na zvaničnom portalu.</p>
      <h5>Dozvola za privremeni boravak</h5>
      <p>Dozvola se izdaje po jednom od propisanih osnova boravka.</p>
      <h5>Kako do dozvole za privremeni boravak i jedinstvenu dozvolu</h5>
      <p>Potrebno je izabrati odgovarajući osnov i priložiti propisanu dokumentaciju.</p>
      <h5>Zahtev za odobrenje privremenog boravka i jedinstvene dozvole</h5>
      <p>Zahtev se pokreće kroz zvaničnu uslugu, a nadležni organ vodi postupak.</p>
      <h5>Kako produžiti dozvolu za privremeni boravak, odnosno jedinstvenu dozvolu</h5>
      <p>Zahtev za produženje podnosi se u rokovima navedenim u zvaničnom uputstvu.</p>
      <h5>Odbijanje zahteva za privremeni boravak, odnosno jedinstvenu dozvolu</h5>
      <p>Zvanična stranica navodi razloge zbog kojih zahtev može biti odbijen.</p>
      <h5>U slučaju nestanka dozvole za privremeni boravak i jedinstvene dozvole</h5>
      <p>Nestanak dozvole prijavljuje se nadležnom organu bez odlaganja.</p>
      <a>Podnesite zahtev</a>
      <footer>{noise} Dinamički sadržaj portala</footer>
    </body></html>
    """


def test_scoped_guidance_ignores_navigation_and_footer_noise() -> None:
    source = _source()
    first = parse_welcome_serbia_guidance(_html(noise="footer-a"), source)
    second = parse_welcome_serbia_guidance(_html(noise="footer-b"), source)

    assert first.external_id == second.external_id
    assert first.body == second.body
    assert "footer-a" not in first.body
    assert "Podnesite zahtev" not in first.body
    assert first.published_at is None
    assert first.url == str(source.url)
    assert first.source_html is not None


def test_guidance_content_change_changes_revision_identity() -> None:
    source = _source()
    before = parse_welcome_serbia_guidance(_html(), source)
    after = parse_welcome_serbia_guidance(
        _html(suffix=" Dodat je novi zvanični uslov postupka."),
        source,
    )

    assert before.external_id != after.external_id
    assert before.canonical_url == after.canonical_url == str(source.url)


def test_missing_required_section_fails_closed() -> None:
    html = _html().replace(
        "<h5>Odbijanje zahteva za privremeni boravak, odnosno jedinstvenu dozvolu</h5>",
        "<h5>Drugi naslov</h5>",
    )
    with pytest.raises(ValueError, match="required section missing: Odbijanje zahteva"):
        parse_welcome_serbia_guidance(html, _source())


def test_high_risk_policy_cannot_be_relaxed() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    with pytest.raises(ValueError, match="high-risk and review-only"):
        parse_welcome_serbia_guidance(_html(), unsafe)


@pytest.mark.asyncio
async def test_factory_collects_scoped_guidance_without_publication_date() -> None:
    source = _source()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "welcometoserbia.gov.rs"
        assert request.url.path == "/privremeni-boravak"
        return httpx.Response(200, text=_html())

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, WelcomeSerbiaGuidanceCollector)
        items = await collector.collect()

    assert len(items) == 1
    assert items[0].published_at is None
    assert items[0].title == GUIDANCE_TITLE


@pytest.mark.asyncio
async def test_cross_host_redirect_is_rejected() -> None:
    source = _source()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/privremeni-boravak"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="unapproved Welcome Serbia guidance URL"):
            await WelcomeSerbiaGuidanceCollector(source, client).collect()

    assert len(requested) == 1
