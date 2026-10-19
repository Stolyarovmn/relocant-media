from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.domain.dedup import canonicalize_url, content_sha256
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.models import CollectorFailure, EditorialRun, Post, RawItem, Source


class ReviewedEvidenceChangedError(ValueError):
    def __init__(self, raw_item_id: int) -> None:
        self.raw_item_id = raw_item_id
        super().__init__(f"reviewed raw item {raw_item_id} changed; new review required")


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
        "ownership": source.ownership,
        "high_risk": source.policy.high_risk,
        "auto_publish": source.policy.auto_publish,
        "enabled": source_is_enabled(source),
    }
    statement = insert(Source).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["id"],
        set_={key: value for key, value in values.items() if key != "id"},
    )
    session.execute(statement)


def _derived_metadata(item: CollectedRawItem) -> tuple[str, str]:
    return canonicalize_url(item.url), content_sha256(item.title, item.body)


def upsert_raw_items(session: Session, items: list[CollectedRawItem]) -> int:
    for item in items:
        canonical_url, fingerprint = _derived_metadata(item)
        existing = session.scalar(select(RawItem).where(
            RawItem.source_id == item.source_id, RawItem.external_id == item.external_id,
        ).with_for_update())
        if existing is not None and session.get(EditorialRun, existing.id) is not None:
            if any(getattr(existing, key) != getattr(item, key)
                   for key in ("url", "title", "body", "published_at")):
                raise ReviewedEvidenceChangedError(existing.id)
            # Derived metadata is safe to backfill: it is deterministic from the exact
            # evidence already reviewed and does not change the evidence snapshot itself.
            existing.canonical_url = canonical_url
            existing.content_sha256 = fingerprint
            continue
        values = {
            "source_id": item.source_id,
            "external_id": item.external_id,
            "url": item.url,
            "canonical_url": canonical_url,
            "content_sha256": fingerprint,
            "title": item.title,
            "body": item.body,
            "published_at": item.published_at,
        }
        statement = insert(RawItem).values(**values)
        statement = statement.on_conflict_do_update(
            constraint="uq_raw_item_source_external",
            set_={
                "url": item.url,
                "canonical_url": canonical_url,
                "content_sha256": fingerprint,
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
