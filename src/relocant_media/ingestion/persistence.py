from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.models import CollectorFailure, RawItem, Source


def source_is_enabled(source: SourceConfig) -> bool:
    return source.extraction.status in {None, "active"}


def sync_source(session: Session, city: str, source: SourceConfig) -> None:
    values = {
        "id": source.id,
        "city": city,
        "name": source.name,
        "url": str(source.url),
        "source_type": source.source_type,
        "trust": Decimal(str(source.trust)),
        "enabled": source_is_enabled(source),
    }
    statement = insert(Source).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["id"],
        set_={key: value for key, value in values.items() if key != "id"},
    )
    session.execute(statement)


def upsert_raw_items(session: Session, items: list[CollectedRawItem]) -> int:
    for item in items:
        values = {
            "source_id": item.source_id,
            "external_id": item.external_id,
            "url": item.url,
            "title": item.title,
            "body": item.body,
            "published_at": item.published_at,
        }
        statement = insert(RawItem).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_raw_item_source_external",
            set_={
                "url": item.url,
                "title": item.title,
                "body": item.body,
                "published_at": item.published_at,
            },
        )
        session.execute(statement)
    return len(items)


def record_collector_failure(
    session: Session,
    source: SourceConfig,
    error: Exception,
) -> None:
    session.add(
        CollectorFailure(
            source_id=source.id,
            strategy=source.extraction.strategy,
            error_type=type(error).__name__,
            message=str(error),
        )
    )
