from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from relocant_media.collectors.base import RawItem
from relocant_media.services.primary_discovery import (
    PublicSearchBudget,
    resolve_public_discovery,
)

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
OFFICIAL = "https://www.beograd.rs/lat/beoinfo-vesti/a116299/radovi.html"


@dataclass(frozen=True)
class Lead:
    text: str
    outbound_urls: tuple[str, ...] = ()


def article(url: str = OFFICIAL) -> RawItem:
    return RawItem(
        source_id="city_beograd_news",
        external_id="116299",
        url=url,
        title="Izmena gradske infrastrukture saobracaja Zvezdara",
        body="Official body",
        published_at=NOW,
    )


@pytest.mark.asyncio
async def test_direct_links_only_resolve_when_authoritative_callback_accepts_them() -> None:
    requested: list[str] = []

    async def resolve_link(url: str) -> str | None:
        requested.append(url)
        return "primary_source_collected" if url == OFFICIAL else None

    async def search_official(source_id: str, url: str) -> list[RawItem]:
        pytest.fail("direct official source should avoid search")

    resolution = await resolve_public_discovery(
        Lead("Izmena gradske infrastrukture saobracaja Zvezdara", (
            "https://evil.example/story",
            OFFICIAL,
        )),
        articles=[],
        resolve_link=resolve_link,
        search_plan=lambda _: [],
        search_official=search_official,
        accept_official=lambda _: True,
        register_official=lambda _: None,
        search_budget=PublicSearchBudget(),
    )

    assert requested == ["https://evil.example/story", OFFICIAL]
    assert resolution.status == "primary_source_resolved"
    assert resolution.primary_urls == (OFFICIAL,)
    assert resolution.primary_results == ("primary_source_collected",)
    assert resolution.searches_performed == 0
    assert resolution.discovery_gaps == ()


@pytest.mark.asyncio
async def test_existing_official_article_is_pointer_not_discovery_evidence() -> None:
    async def resolve_link(url: str) -> str | None:
        return None

    async def search_official(source_id: str, url: str) -> list[RawItem]:
        pytest.fail("matching independently collected article should avoid search")

    existing = article()
    resolution = await resolve_public_discovery(
        Lead("Izmena gradske infrastrukture saobracaja Zvezdara"),
        articles=[existing],
        resolve_link=resolve_link,
        search_plan=lambda _: [],
        search_official=search_official,
        accept_official=lambda _: True,
        register_official=lambda _: None,
        search_budget=PublicSearchBudget(),
    )

    assert resolution.status == "primary_source_resolved"
    assert resolution.primary_urls == (OFFICIAL,)
    assert resolution.primary_results == ("primary_source_collected",)


@pytest.mark.asyncio
async def test_public_search_budget_is_shared_and_fail_closed() -> None:
    budget = PublicSearchBudget(limit=1)
    searches: list[tuple[str, str]] = []
    registered: list[RawItem] = []

    async def resolve_link(url: str) -> str | None:
        return None

    async def search_official(source_id: str, url: str) -> list[RawItem]:
        searches.append((source_id, url))
        return []

    first = await resolve_public_discovery(
        Lead("Prva nepovezana javna tema"),
        articles=[],
        resolve_link=resolve_link,
        search_plan=lambda _: [("venue_a", "https://venue-a.example/search?q=prva")],
        search_official=search_official,
        accept_official=lambda _: True,
        register_official=registered.append,
        search_budget=budget,
    )
    second = await resolve_public_discovery(
        Lead("Druga nepovezana javna tema"),
        articles=[],
        resolve_link=resolve_link,
        search_plan=lambda _: [("venue_b", "https://venue-b.example/search?q=druga")],
        search_official=search_official,
        accept_official=lambda _: True,
        register_official=registered.append,
        search_budget=budget,
    )

    assert first.status == "deferred_missing_primary_source"
    assert first.searches_performed == 1
    assert first.discovery_gaps == ("search_without_accepted_match",)
    assert second.status == "deferred_missing_primary_source"
    assert second.searches_performed == 0
    assert second.discovery_gaps == ("search_budget_exhausted",)
    assert searches == [("venue_a", "https://venue-a.example/search?q=prva")]
    assert registered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://unknown.example/notice?private_value=do-not-store",
    "https://n1info.rs/",  # Известный домен, но не статья.
])
async def test_unsupported_links_are_diagnosed_without_becoming_evidence(url: str) -> None:
    from pathlib import Path

    from relocant_media.services.primary_discovery import source_for_url
    from relocant_media.sources.registry import load_source_file

    source = load_source_file(Path("cities/belgrade/sources/media_n1_srbija.yaml"))

    async def resolve_link(url: str) -> str | None:
        assert source_for_url(url, {source.id: source}) is None
        return None

    async def search_official(source_id: str, url: str) -> list[RawItem]:
        pytest.fail("no configured search plan")

    resolution = await resolve_public_discovery(
        Lead("Тема без поддерживаемого URL", (url,)),
        articles=[], resolve_link=resolve_link, search_plan=lambda _: [],
        search_official=search_official, accept_official=lambda _: True,
        register_official=lambda _: pytest.fail("unsupported URL must not be registered"),
        search_budget=PublicSearchBudget(),
    )
    assert resolution.status == "deferred_missing_primary_source"
    assert resolution.primary_urls == ()
    assert resolution.primary_results == ()
    assert resolution.discovery_gaps == (
        "outbound_url_not_supported", "no_configured_search_plan",
    )
    assert "do-not-store" not in repr(resolution)


@pytest.mark.asyncio
async def test_search_candidate_requires_topic_match_and_acceptance() -> None:
    registered: list[RawItem] = []
    candidate = article("https://official.example/event")

    async def resolve_link(url: str) -> str | None:
        return None

    async def search_official(source_id: str, url: str) -> list[RawItem]:
        return [candidate]

    rejected = await resolve_public_discovery(
        Lead("Izmena gradske infrastrukture saobracaja Zvezdara"),
        articles=[],
        resolve_link=resolve_link,
        search_plan=lambda _: [("venue", "https://official.example/search?q=izmena")],
        search_official=search_official,
        accept_official=lambda _: False,
        register_official=registered.append,
        search_budget=PublicSearchBudget(),
    )

    assert rejected.status == "deferred_missing_primary_source"
    assert registered == []
