from pathlib import Path

import httpx
import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.bitefartcafe_events import (
    BitefArtCafeEventsCollector,
    _concert_links,
    parse_bitefartcafe_event,
)
from relocant_media.collectors.factory import build_collector
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.services.event_place import resolve_event_place
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/venue_bitefartcafe.yaml")
LIST_PATH = "/program/koncerti/"
FIRST_PATH = "/dogadjaji/psihomodo-pop/"
SECOND_PATH = "/dogadjaji/kovacs-musicology-barcaffe-sessions/"
FIRST_URL = f"https://bitefartcafe.rs{FIRST_PATH}"


def _source():
    return load_source_file(SOURCE_PATH)


def _listing() -> str:
    return f"""
    <html><body>
      <div>Koncerti</div>
      <a href="{FIRST_PATH}">
        <h2>PSIHOMODO POP</h2><span>30.10.2026.</span><span>22h</span>
      </a>
      <a href="{SECOND_PATH}">
        <h2>KOVACS - Musicology Barcaffè sessions</h2>
        <span>04.11.2026.</span><span>21h</span>
      </a>
      <a href="/kontakt/">Kontakt</a>
    </body></html>
    """


def _detail(title: str = "PSIHOMODO POP", date: str = "30.10.2026.") -> str:
    body = (
        "BitefArtCafe objavljuje zvaničan koncertni program sa uslovima ulaska, "
        "terminom, informacijama o ulaznicama i detaljima događaja za publiku. "
        * 3
    )
    return f"""
    <html>
      <head><title>{title} - BitefArtCafe</title></head>
      <body>
        <div>{date}</div><div>22h</div><div>BITEFARTCAFE</div>
        <h2>{title}</h2><h2>{title}</h2>
        <div>{body}</div>
        <div>Na koncertima postoji starosno ograničenje 18+.</div>
        <h2>{title}</h2>
        <h2>Online Rezervacije</h2>
        <p>Ovaj tekst ne sme da uđe u telo dokaza.</p>
      </body>
    </html>
    """


def test_listing_uses_only_canonical_event_detail_links() -> None:
    links = _concert_links(_listing(), _source())

    assert links == [
        "https://bitefartcafe.rs/dogadjaji/psihomodo-pop/",
        "https://bitefartcafe.rs/dogadjaji/kovacs-musicology-barcaffe-sessions/",
    ]


def test_detail_keeps_event_date_but_does_not_invent_publication_date() -> None:
    item = parse_bitefartcafe_event(_detail(), FIRST_URL, _source())

    assert item.title == "PSIHOMODO POP"
    assert item.published_at is None
    assert item.body.startswith("Datum događaja: 30.10.2026.\n")
    assert "starosno ograničenje 18+" in item.body
    assert "Ovaj tekst ne sme" not in item.body
    assert item.external_id == item.canonical_url == item.url == FIRST_URL
    assert item.source_html == _detail()


def test_trailing_duplicate_heading_does_not_hide_substantive_body() -> None:
    item = parse_bitefartcafe_event(
        _detail("MIŠKO PLAVI TRIO", "25.11.2026."), FIRST_URL, _source()
    )

    assert item.title == "MIŠKO PLAVI TRIO"
    assert item.body.startswith("Datum događaja: 25.11.2026.\n")
    assert "starosno ograničenje 18+" in item.body
    assert len(item.body) >= 120


def _event_card(date: str, address: str, ticket_url: str) -> str:
    return f"""
    <div class="elementor-column">
      <p>{date}</p><p>Vrata otvaramo u 20:30</p><p>Program traje od 21h</p>
      <p>Lokacija:</p><h2>BITEFARTCAFE</h2><h2>{address}<br>11000 Beograd</h2>
      <p>Cena Ulaznice:</p><h2>od 3.000rsd</h2>
      <a href="{ticket_url}">Kupite ulaznice</a>
      <p>Informacije i rezervacije:</p><p>Telefon za rezervacije</p>
    </div>
    """


def _detail_with_cards(*cards: str) -> str:
    return _detail().replace(
        "<h2>Online Rezervacije</h2>",
        '<div class="elementor-container">'
        '<div class="elementor-column"><h2>Online Rezervacije</h2></div>'
        f'{"".join(cards)}</div>',
    )


def test_detail_keeps_only_practical_facts_from_its_own_event_card() -> None:
    tickets = "https://bilet.rs/events/455/concert"
    item = parse_bitefartcafe_event(
        _detail_with_cards(
            _event_card("30.10.2026.", "Mitropolita Petra 8", tickets),
            _event_card("31.10.2026.", "Other venue", "https://bilet.rs/events/456/concert"),
        ),
        FIRST_URL,
        _source(),
    )
    assert "Vrata otvaramo u 20:30" in item.body
    assert "Mitropolita Petra 8\n11000 Beograd" in item.body
    assert "Cena Ulaznice:\nod 3.000rsd" in item.body
    assert f"Kupite ulaznice: {tickets}" in item.body
    assert "Other venue" not in item.body
    assert "Telefon za rezervacije" not in item.body
    assert item.published_at is None


def test_ambiguous_cards_do_not_supply_an_address_or_price() -> None:
    item = parse_bitefartcafe_event(
        _detail_with_cards(
            _event_card("30.10.2026.", "Address one", "#"),
            _event_card("30.10.2026.", "Address two", "#"),
        ),
        FIRST_URL,
        _source(),
    )
    assert "Address" not in item.body
    assert "Cena Ulaznice" not in item.body
    assert "starosno ograničenje 18+" in item.body


def test_card_does_not_preserve_a_nonpublic_ticket_target() -> None:
    item = parse_bitefartcafe_event(
        _detail_with_cards(
            _event_card("30.10.2026.", "Mitropolita Petra 8", "http://localhost/buy")
        ),
        FIRST_URL,
        _source(),
    )
    assert "Mitropolita Petra 8" in item.body
    assert "localhost" not in item.body


def _map_snapshot(html: str) -> EvidenceDocument:
    item = parse_bitefartcafe_event(html, FIRST_URL, _source())
    return EvidenceDocument.model_validate(item, from_attributes=True)


def test_concert_map_uses_program_start_and_verified_club_point() -> None:
    html = _detail_with_cards(
        _event_card("30.10.2026.", "Mitropolita Petra 8", "https://bilet.rs/events/455/concert")
    )
    place = resolve_event_place(_map_snapshot(html), html, profile=load_city_profile("belgrade"))
    assert place is not None
    assert place["starts_at"] == "2026-10-30T21:00:00+01:00"
    assert place["ends_at"] is None
    assert place["venue"]["address"] == "Mitropolita Petra 8, Beograd"
    assert place["coordinate_source_url"] == "https://www.openstreetmap.org/node/6844070707"
    assert place["schedule_source_url"] == FIRST_URL


@pytest.mark.parametrize("new_day", ["30", "31"])
def test_concert_map_requires_the_card_date_to_match_the_reschedule(new_day: str) -> None:
    html = _detail_with_cards(_event_card("30.10.2026.", "Mitropolita Petra 8", "#")).replace(
        "Na koncertima postoji",
        f"Koncert, planiran za 29. oktobar, pomera na {new_day}. oktobar. Na koncertima postoji",
    )
    place = resolve_event_place(_map_snapshot(html), html, profile=load_city_profile("belgrade"))
    if new_day == "30":
        assert place is not None
        assert place["starts_at"] == "2026-10-30T21:00:00+01:00"
    else:
        assert place is None


@pytest.mark.parametrize("old_value,new_value", [
    ("Mitropolita Petra 8", "Mali Kalemegdan 2"),
    ("Program traje od 21h", "Vrata otvaramo u 21h"),
    ("Program traje od 21h", "Program traje od 90h"),
    ("Na koncertima postoji", "Koncert je otkazan. Na koncertima postoji"),
])
def test_concert_map_rejects_unproven_address_start_or_cancellation(
    old_value: str, new_value: str,
) -> None:
    html = _detail_with_cards(_event_card("30.10.2026.", "Mitropolita Petra 8", "#"))
    html = html.replace(old_value, new_value)
    assert resolve_event_place(
        _map_snapshot(html), html, profile=load_city_profile("belgrade"),
    ) is None


def test_concert_map_rejects_changes_after_evidence_was_saved() -> None:
    html = _detail_with_cards(_event_card("30.10.2026.", "Mitropolita Petra 8", "#"))
    assert resolve_event_place(
        _map_snapshot(html), html.replace("Program traje od 21h", "Program traje od 22h"),
        profile=load_city_profile("belgrade"),
    ) is None


@pytest.mark.asyncio
async def test_factory_fetches_full_official_details() -> None:
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
                    "KOVACS - Musicology Barcaffè sessions",
                    "04.11.2026.",
                ),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = build_collector(source, client)
        assert isinstance(collector, BitefArtCafeEventsCollector)
        items = await collector.collect()

    assert len(items) == 2
    assert requested == [LIST_PATH, FIRST_PATH, SECOND_PATH]
    assert all(item.published_at is None for item in items)


@pytest.mark.asyncio
async def test_auto_publish_and_cross_host_redirect_are_rejected() -> None:
    source = _source()
    unsafe = source.model_copy(
        update={"policy": source.policy.model_copy(update={"auto_publish": True})}
    )
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(ValueError, match="must stay review-only"):
            BitefArtCafeEventsCollector(unsafe, client)

    requested: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://example.com/program/koncerti/"},
        )

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(redirect),
    ) as client:
        with pytest.raises(ValueError, match="unapproved BitefArtCafe URL"):
            await BitefArtCafeEventsCollector(source, client).collect()

    assert len(requested) == 1


@pytest.mark.asyncio
async def test_live_review_fetches_the_exact_card_without_a_program_scan() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=_detail())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        item = await BitefArtCafeEventsCollector(_source(), client).collect_url(FIRST_URL)

    assert requested == [FIRST_URL]
    assert item.url == FIRST_URL
    assert item.published_at is None


def test_source_is_structured_review_only_without_rss() -> None:
    source = _source()

    assert source.extraction.strategy == "bitefartcafe_events"
    assert source.extraction.feed_url is None
    assert source.extraction.approved_hosts == (
        "bitefartcafe.rs",
        "www.bitefartcafe.rs",
    )
    assert source.policy.auto_publish is False
    assert str(source.url) == "https://bitefartcafe.rs/program/koncerti/"
