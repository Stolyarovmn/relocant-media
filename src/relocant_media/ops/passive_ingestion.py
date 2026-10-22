"""Persist active review-only source evidence without entering the autonomous queue."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.collectors.factory import build_collector
from relocant_media.ingestion.persistence import (
    capture_collected_batch,
    record_collector_failure,
    source_is_enabled,
    sync_source,
)
from relocant_media.sources.registry import SourceConfig, load_source_registry

DEFAULT_SOURCES = Path("cities/belgrade/sources")
_MAX_CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class PassiveSourceResult:
    source_id: str
    item_count: int = 0
    review_required: int = 0
    item_failure_count: int = 0
    failed: bool = False


async def _collect_one(
    source: SourceConfig,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
) -> tuple[SourceConfig, list[Any] | None, int, Exception | None]:
    async with semaphore:
        try:
            collector = build_collector(source, client)
            items = await collector.collect()
            item_failures = getattr(collector, "item_failures", [])
            return source, items, len(item_failures), None
        except Exception as error:
            return source, None, 0, error


def passive_sources(
    directory: Path = DEFAULT_SOURCES,
) -> tuple[SourceConfig, ...]:
    """Review-only active sources are evidence inputs, never autonomous candidates."""
    return tuple(
        source
        for source in load_source_registry(directory)
        if source_is_enabled(source) and not source.policy.auto_publish
    )


async def collect_passive_sources_async(
    engine: Engine,
    *,
    city: str,
    directory: Path = DEFAULT_SOURCES,
) -> dict[str, Any]:
    """Collect review-only sources concurrently, then persist each source atomically."""
    sources = passive_sources(directory)
    if not sources:
        return {
            "passive_sources": 0,
            "passive_items": 0,
            "passive_review_required": 0,
            "passive_failures": 0,
            "passive_item_failures": 0,
            "sources": [],
        }

    # Sync source policy before collection so persisted failures and raw evidence always
    # reference the exact current high-risk/auto-publish flags.
    with Session(engine) as session:
        for source in sources:
            sync_source(session, city, source)
        session.commit()

    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        collected = await asyncio.gather(
            *(_collect_one(source, client, semaphore) for source in sources)
        )

    results: list[PassiveSourceResult] = []
    for source, items, item_failure_count, error in collected:
        with Session(engine) as session:
            if error is not None:
                record_collector_failure(session, source, error)
                session.commit()
                results.append(PassiveSourceResult(source_id=source.id, failed=True))
                continue
            assert items is not None
            written, review_required = capture_collected_batch(session, items)
            session.commit()
            results.append(
                PassiveSourceResult(
                    source_id=source.id,
                    item_count=written,
                    review_required=review_required,
                    item_failure_count=item_failure_count,
                )
            )

    return {
        "passive_sources": len(results),
        "passive_items": sum(result.item_count for result in results),
        "passive_review_required": sum(result.review_required for result in results),
        "passive_failures": sum(result.failed for result in results),
        "passive_item_failures": sum(result.item_failure_count for result in results),
        "sources": [asdict(result) for result in results],
    }


def collect_passive_sources(
    engine: Engine,
    *,
    city: str,
    directory: Path = DEFAULT_SOURCES,
) -> dict[str, Any]:
    """Synchronous entry point for the newsroom primary cycle."""
    return asyncio.run(collect_passive_sources_async(engine, city=city, directory=directory))
