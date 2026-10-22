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
    assert second.status == "deferred_missing_primary_source"
    assert second.searches_performed == 0
    assert searches == [("venue_a", "https://venue-a.example/search?q=prva")]
    assert registered == []


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
