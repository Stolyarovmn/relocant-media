"""Materialize reviewed source revisions as fresh editorial candidates with lineage."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.dedup import EventCandidate, canonicalize_url, content_sha256, event_key
from relocant_media.storage.models import EditorialRun, RawItem, Source, SourceRevision
from relocant_media.storage.revision_control import (
    SourceRevisionDecision,
    SourceRevisionMaterialization,
)


def _candidate(item: RawItem) -> EventCandidate:
    return EventCandidate(
        raw_item_id=item.id,
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        body=item.body,
        published_at=item.published_at,
        canonical_url=item.canonical_url,
        content_sha256=item.content_sha256,
    )


def _versioned_canonical_url(url: str, snapshot_sha256: str) -> str:
    """Keep the public URL intact while giving a changed snapshot a new dedup identity."""
    canonical = canonicalize_url(url)
    separator = "&" if "?" in canonical else "?"
    return f"{canonical}{separator}__source_revision={snapshot_sha256[:16]}"


def _latest_decision(
    session: Session, revision_id: int
) -> SourceRevisionDecision | None:
    return session.scalar(
        select(SourceRevisionDecision)
        .where(SourceRevisionDecision.revision_id == revision_id)
        .order_by(
            SourceRevisionDecision.decided_at.desc(),
            SourceRevisionDecision.id.desc(),
        )
        .limit(1)
    )


def materialize_source_revision(session: Session, revision_id: int) -> RawItem:
    """Create one immutable fresh candidate for a queued source revision.

    The old RawItem and EditorialRun are never rewritten. The candidate keeps the exact official
    URL for citation, but gets a versioned internal canonical identity so a changed body/title can
    pass the stale-event duplicate guard and receive a complete new editorial review.
    """
    existing = session.get(SourceRevisionMaterialization, revision_id)
    if existing is not None:
        item = session.get(RawItem, existing.raw_item_id)
        if item is None:
            raise ValueError("materialized source revision raw item is missing")
        return item

    revision = session.get(SourceRevision, revision_id)
    if revision is None:
        raise ValueError("source revision does not exist")
    decision = _latest_decision(session, revision_id)
    if decision is None or decision.decision != "queue_reverify":
        raise ValueError("source revision is not queued for re-verification")

    parent = session.get(RawItem, revision.raw_item_id)
    if parent is None:
        raise ValueError("source revision parent raw item is missing")
    source = session.get(Source, parent.source_id)
    if source is None or not source.enabled:
        raise ValueError("source revision source is missing or disabled")

    evidence = revision.evidence
    document = EvidenceDocument.model_validate(evidence)
    article_id = evidence.get("article_id")
    if not isinstance(article_id, str) or not article_id.strip():
        raise ValueError("source revision article_id is missing")
    if document.source_id != parent.source_id:
        raise ValueError("source revision source does not match parent")

    item = RawItem(
        source_id=parent.source_id,
        external_id=f"{article_id}::revision:{revision.id}",
        url=document.url,
        canonical_url=_versioned_canonical_url(document.url, revision.snapshot_sha256),
        content_sha256=content_sha256(document.title, document.body),
        title=document.title,
        body=document.body,
        published_at=document.published_at,
    )
    session.add(item)
    session.flush()
    session.add(
        SourceRevisionMaterialization(
            revision_id=revision.id,
            raw_item_id=item.id,
            parent_raw_item_id=parent.id,
        )
    )
    session.flush()
    return item


def source_revision_lifecycle_context(
    session: Session, raw_item_id: int
) -> dict[str, Any] | None:
    """Return immutable event lineage for a materialized revision candidate."""
    materialization = session.scalar(
        select(SourceRevisionMaterialization).where(
            SourceRevisionMaterialization.raw_item_id == raw_item_id
        )
    )
    if materialization is None:
        return None
    item = session.get(RawItem, materialization.raw_item_id)
    parent = session.get(RawItem, materialization.parent_raw_item_id)
    revision = session.get(SourceRevision, materialization.revision_id)
    if item is None or parent is None or revision is None:
        raise ValueError("source revision lifecycle lineage is incomplete")
    run = session.get(EditorialRun, item.id)
    return {
        "kind": "source_update",
        "revision_id": revision.id,
        "snapshot_sha256": revision.snapshot_sha256,
        "parent_raw_item_id": parent.id,
        "raw_item_id": item.id,
        "parent_event_key": event_key(_candidate(parent)),
        "event_key": event_key(_candidate(item)),
        "fresh_editorial_review_created": run is not None,
        "delivery_status": None if run is None else run.delivery_status,
    }
