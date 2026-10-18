from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from relocant_media.collectors.factory import build_collector
from relocant_media.ingestion.persistence import (
    record_collector_failure,
    source_is_enabled,
    sync_source,
    upsert_raw_items,
)
from relocant_media.sources.registry import load_source_registry


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    sources_synced: int
    sources_skipped: int
    raw_items_written: int
    failures: int


async def ingest_city(
    city: str,
    source_directory: Path,
    session: Session,
    client: httpx.AsyncClient,
) -> IngestionSummary:
    sources = load_source_registry(source_directory)
    for source in sources:
        sync_source(session, city, source)
    session.commit()

    skipped = 0
    raw_items_written = 0
    failures = 0

    for source in sources:
        if not source_is_enabled(source):
            skipped += 1
            continue

        try:
            collector = build_collector(source, client)
            items = await collector.collect()
            raw_items_written += upsert_raw_items(session, items)
            session.commit()
        except Exception as error:
            session.rollback()
            record_collector_failure(session, source, error)
            session.commit()
            failures += 1

    return IngestionSummary(
        sources_synced=len(sources),
        sources_skipped=skipped,
        raw_items_written=raw_items_written,
        failures=failures,
    )
