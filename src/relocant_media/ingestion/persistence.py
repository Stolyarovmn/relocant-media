from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.models import CollectorFailure, EditorialRun, Post, RawItem, Source


class ReviewedEvidenceChangedError(ValueError):
    def __init__(self, raw_item_id: int) -> None:
        self.raw_item_id = raw_item_id
        super().__init__(f"reviewed raw item {raw_item_id} changed; new review required")


def _evidence_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
    return value


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
    insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgres_insert
    statement = insert(Source).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["id"],
        set_={key: value for key, value in values.items() if key != "id"},
    )
    session.execute(statement)


def upsert_raw_items(session: Session, items: list[CollectedRawItem]) -> int:
    for item in items:
        existing = session.scalar(select(RawItem).where(
            RawItem.source_id == item.source_id, RawItem.external_id == item.external_id,
        ).with_for_update())
        if existing is not None and session.get(EditorialRun, existing.id) is not None:
            if any(_evidence_value(getattr(existing, key)) != _evidence_value(getattr(item, key))
                   for key in ("url", "title", "body", "published_at")):
                raise ReviewedEvidenceChangedError(existing.id)
            continue
        values = {
            "source_id": item.source_id,
            "external_id": item.external_id,
            "url": item.url,
            "title": item.title,
            "body": item.body,
            "published_at": _evidence_value(item.published_at),
        }
        insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else postgres_insert
        statement = insert(RawItem).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["source_id", "external_id"],
            set_={
                "url": item.url,
                "title": item.title,
                "body": item.body,
                "published_at": _evidence_value(item.published_at),
            },
        )
        session.execute(statement)
    return len(items)


def record_collector_failure(
    session: Session,
    source: SourceConfig,
    error: Exception,
) -> None:
    if isinstance(error, ReviewedEvidenceChangedError):
        run = session.get(EditorialRun, error.raw_item_id)
        if run is not None:
            run.outcome = {**run.outcome, "status": "human_review",
                           "reason": "source_changed_requires_review"}
            post = session.get(Post, run.post_id) if run.post_id else None
            if post is not None and post.published_at is None:
                post.status = "human_review"
    session.add(
        CollectorFailure(
            source_id=source.id,
            strategy=source.extraction.strategy,
            error_type=type(error).__name__,
            message=str(error),
        )
    )
