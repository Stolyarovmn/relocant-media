from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from relocant_media.collectors.factory import build_collector
from relocant_media.ingestion.persistence import (
    capture_collected_batch,
    record_collector_failure,
    source_is_enabled,
    sync_source,
)
from relocant_media.sources.registry import load_source_registry


@dataclass(frozen=True, slots=True)
class IngestionSummary:
    sources_synced: int
    sources_skipped: int
    raw_items_written: int
    failures: int
    review_required: int = 0


async def ingest_city(
    city: str,
    source_directory: Path,
    session: Session,
    client: httpx.AsyncClient,
    *,
    source_ids: tuple[str, ...] | None = None,
) -> IngestionSummary:
    sources = load_source_registry(source_directory)
    if source_ids is not None:
        unknown = set(source_ids) - {source.id for source in sources}
        if not source_ids or unknown:
            raise ValueError(f"nonempty known source ids required; unknown: {sorted(unknown)}")
        sources = tuple(source for source in sources if source.id in source_ids)
    for source in sources:
        sync_source(session, city, source)
    session.commit()

    skipped = 0
    raw_items_written = 0
    failures = 0
    review_required = 0

    for source in sources:
        if not source_is_enabled(source):
            skipped += 1
            continue

        try:
            collector = build_collector(source, client)
            items = await collector.collect()
            written, changes = capture_collected_batch(session, items)
            session.commit()
            raw_items_written += written
            review_required += changes
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
        review_required=review_required,
    )
