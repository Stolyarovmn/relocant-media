import hashlib
import json
from datetime import UTC
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.domain.dedup import canonicalize_url, content_sha256
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import (
    CollectorFailure,
    EditorialRun,
    Post,
    RawItem,
    Source,
    SourceRevision,
)


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
    statement = (sqlite_insert(Source) if session.get_bind().dialect.name == "sqlite"
                 else insert(Source)).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=["id"],
        set_={key: value for key, value in values.items() if key != "id"},
    )
    session.execute(statement)


def _derived_metadata(item: CollectedRawItem) -> tuple[str, str]:
    canonical_url = item.canonical_url or canonicalize_url(item.url)
    return canonical_url, content_sha256(item.title, item.body)


def _referencing_runs(session: Session, raw_item_id: int) -> list[EditorialRun]:
    if session.get_bind().dialect.name == "sqlite":
        # JSONB contains is PostgreSQL-specific. Compare exact integer IDs,
        # preserving secondary-document holds for the bounded local newsroom.
        return [run for run in session.scalars(select(EditorialRun).execution_options(
            populate_existing=True,
        )) if run.raw_item_id == raw_item_id or any(
            isinstance(document, dict) and document.get("raw_item_id") == raw_item_id
            for document in run.evidence.get("documents", [])
        )]
    return list(session.scalars(select(EditorialRun).where(or_(
        EditorialRun.raw_item_id == raw_item_id,
        EditorialRun.evidence["documents"].contains([{"raw_item_id": raw_item_id}]),
    )).execution_options(populate_existing=True)))


def _block_changed_evidence(session: Session, raw_item_id: int) -> None:
    for run in _referencing_runs(session, raw_item_id):
        post = session.get(Post, run.post_id) if run.post_id else None
        already_sent = run.delivery_status == "sent" or bool(
            post is not None
            and (post.published_at is not None or post.telegram_message_id is not None)
        )
        if already_sent:
            # Source history may evolve after publication. Keep the immutable receipt/outcome
            # of what was actually sent; the new snapshot is handled as a separate revision.
            continue
        run.outcome = {**run.outcome, "status": "human_review",
                       "reason": "source_changed_requires_review"}
        if post is not None and post.published_at is None:
            post.status = "human_review"


def upsert_raw_items(session: Session, items: list[CollectedRawItem]) -> int:
    cities = session.scalars(select(Source.city).where(
        Source.id.in_({item.source_id for item in items}),
    ).distinct())
    for city in sorted(cities):
        lock_editorial_city(session, city)
    for item in items:
        canonical_url, fingerprint = _derived_metadata(item)
        existing = session.scalar(select(RawItem).where(
            RawItem.source_id == item.source_id, RawItem.external_id == item.external_id,
        ).with_for_update().execution_options(populate_existing=True))
        if existing is not None and _referencing_runs(session, existing.id):
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
        statement = (sqlite_insert(RawItem) if session.get_bind().dialect.name == "sqlite"
                     else insert(RawItem)).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["source_id", "external_id"],
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


def capture_collected_batch(
    session: Session, items: list[CollectedRawItem],
) -> tuple[int, int]:
    """Caller commits the entire batch; changes are queued, never approved here."""
    # Acquire all city locks in a stable order before taking any raw-item locks.
    cities = session.scalars(select(Source.city).where(
        Source.id.in_({item.source_id for item in items}),
    ).distinct())
    for city in sorted(cities):
        lock_editorial_city(session, city)
    written = review_required = 0
    for item in items:
        try:
            written += upsert_raw_items(session, [item])
        except ReviewedEvidenceChangedError as error:
            evidence = {
                "source_id": item.source_id, "article_id": item.external_id,
                "url": item.url, "title": item.title, "body": item.body,
                "published_at": (item.published_at.astimezone(UTC).isoformat()
                                 if item.published_at is not None else None),
            }
            encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
            statement = (sqlite_insert(SourceRevision)
                         if session.get_bind().dialect.name == "sqlite" else insert(SourceRevision))
            session.execute(statement.values(
                raw_item_id=error.raw_item_id,
                snapshot_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                evidence=evidence,
            ).on_conflict_do_nothing(index_elements=["raw_item_id", "snapshot_sha256"]))
            _block_changed_evidence(session, error.raw_item_id)
            review_required += 1
    return written, review_required


def record_collector_failure(
    session: Session,
    source: SourceConfig,
    error: Exception,
) -> None:
    if isinstance(error, ReviewedEvidenceChangedError):
        _block_changed_evidence(session, error.raw_item_id)
    session.add(
        CollectorFailure(
            source_id=source.id,
            strategy=source.extraction.strategy,
            error_type=type(error).__name__,
            message=str(error),
        )
    )
