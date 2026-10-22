from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from relocant_media.collectors.beoelektrane_network_works import (
    BeoelektraneNetworkWorksCollector,
    parse_beoelektrane_network_works,
)
from relocant_media.sources.registry import SourceConfig

BELGRADE_TZ = ZoneInfo("Europe/Belgrade")
PAGE_URL = "https://beoelektrane.co.rs/planirani-radovi/radovi-na-toplovodnoj-mrezi/"


def _source() -> SourceConfig:
    return SourceConfig.model_validate(
        {
            "id": "beoelektrane_network_works",
            "name": "JKP Beogradske elektrane — Radovi na toplovodnoj mreži",
            "publisher": "JKP Beogradske elektrane",
            "ownership": "official",
            "url": PAGE_URL,
            "language": "sr-Latn",
            "source_type": "official_heating_service_info",
            "trust": 1.0,
            "check_every": "1h",
            "categories": ["utilities", "heating"],
            "extraction": {"strategy": "beoelektrane_network_works", "status": "active"},
            "policy": {"high_risk": False, "auto_publish": False},
        }
    )


def _html(*, first_street: str = "Crnotravskoj ulici") -> str:
    return f"""
    <html><body>
      <nav>Planirani radovi na mreži</nav>
      <main>
        <h1>Radovi na toplovodnoj mreži</h1>
        <h2>07 авг 2026 - Radovi na toplovodnoj mreži</h2>
        <div>Posted at 08:22h in Planirani radovi by Svetlana Stojakovic</div>
        <h4>Radovi na toplovodnoj mreži – Voždovac</h4>
        <p>-Radovi na izgradnji toplovodne mreže u {first_street}. Radovi se izvode po fazama.</p>
        <p>-Radovi na izgradnji toplovodne mreže u Mitrovdanskoj ulici.</p>
        <h4>Radovi na toplovodnoj mreži – Stari grad</h4>
        <p>– Radovi na toplovodnoj mreži u Dobračinoj ulici.</p>
        <p>-Radovi na toplovodnoj mreži na uglu Dunavske i Kapetan Mišine ulice.</p>
        <h2>Radovi na toplovodnoj mreži – Savski venac</h2>
        <p>-Radovi na toplovodnoj mreži u Neznanog junaka 2a – 4.</p>
        <p>JKP „Beogradske elektrane“ zahvaljuju se sugrađanima na razumevanju i strpljenju.</p>
      </main>
      <footer>011/2093-000</footer>
    </body></html>
    """


def test_parser_splits_explicit_districts_and_publication_time() -> None:
    items = parse_beoelektrane_network_works(_html(), _source(), page_url=PAGE_URL)
    expected_published_at = datetime(2026, 8, 7, 8, 22, tzinfo=BELGRADE_TZ)

    assert [item.title for item in items] == [
        "Radovi na toplovodnoj mreži – Voždovac",
        "Radovi na toplovodnoj mreži – Stari grad",
        "Radovi na toplovodnoj mreži – Savski venac",
    ]
    assert all(item.url == PAGE_URL for item in items)
    assert all(item.published_at == expected_published_at for item in items)
    assert all("__beoelektrane_notice=" in str(item.canonical_url) for item in items)
    assert "Mitrovdanskoj" in items[0].body
    assert "Dobračinoj" in items[1].body


def test_duplicate_identical_date_and_time_metadata_is_accepted() -> None:
    metadata = """
        <h2>07 авг 2026 - Radovi na toplovodnoj mreži</h2>
        <div>Posted at 08:22h in Planirani radovi by Svetlana Stojakovic</div>
    """
    html = _html().replace(metadata, metadata + metadata, 1)

    items = parse_beoelektrane_network_works(html, _source())

    assert items[0].published_at == datetime(2026, 8, 7, 8, 22, tzinfo=BELGRADE_TZ)


def test_conflicting_publication_dates_fail_closed() -> None:
    html = _html().replace(
        "<div>Posted at 08:22h in Planirani radovi by Svetlana Stojakovic</div>",
        """
        <h2>08 авг 2026 - Radovi na toplovodnoj mreži</h2>
        <div>Posted at 08:22h in Planirani radovi by Svetlana Stojakovic</div>
        """,
        1,
    )

    with pytest.raises(ValueError, match="publication date conflicting"):
        parse_beoelektrane_network_works(html, _source())


def test_same_date_and_district_keep_identity_across_street_revision() -> None:
    before = parse_beoelektrane_network_works(_html(), _source())[0]
    after = parse_beoelektrane_network_works(
        _html(first_street="Bulevaru oslobođenja"),
        _source(),
    )[0]

    assert before.external_id == after.external_id
    assert before.canonical_url == after.canonical_url
    assert before.body != after.body


def test_cyrillic_october_date_without_posted_time_is_explicit_date_only() -> None:
    html = """
    <main>
      <h1>Radovi na toplovodnoj mreži</h1>
      <h2>16 окт 2026 - Radovi na toplovodnoj mreži</h2>
      <h2>Radovi na toplovodnoj mreži – Zemun</h2>
      <p>-Radovi na izgradnji toplovoda duž Ulice Teodora Hercla.</p>
      <h4>Radovi na toplovodnoj mreži – Voždovac</h4>
      <p>-Radovi na izgradnji toplovodne mreže u Mitrovdanskoj i Ulici braće Jerković.</p>
      <p>JKP „Beogradske elektrane“ zahvaljuju se sugrađanima na razumevanju i strpljenju.</p>
    </main>
    """

    items = parse_beoelektrane_network_works(html, _source())

    assert items[0].published_at == datetime(2026, 10, 16, tzinfo=BELGRADE_TZ)
    assert items[0].title.endswith("Zemun")
    assert items[1].title.endswith("Voždovac")


def test_parser_fails_closed_without_footer_sentinel() -> None:
    html = _html().replace(
        "JKP „Beogradske elektrane“ zahvaljuju se sugrađanima na razumevanju i strpljenju.",
        "",
    )

    with pytest.raises(ValueError, match="footer sentinel"):
        parse_beoelektrane_network_works(html, _source())


def test_parser_rejects_duplicate_district_heading() -> None:
    html = _html().replace(
        "<h4>Radovi na toplovodnoj mreži – Stari grad</h4>",
        "<h4>Radovi na toplovodnoj mreži – Voždovac</h4>",
    )

    with pytest.raises(ValueError, match="duplicate"):
        parse_beoelektrane_network_works(html, _source())


@pytest.mark.asyncio
async def test_collector_rejects_redirect_to_external_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/works"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = BeoelektraneNetworkWorksCollector(_source(), client)
        with pytest.raises(ValueError, match="unapproved Beogradske elektrane URL"):
            await collector.collect()
