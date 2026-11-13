"""Готовит новую редакционную проверку изменившегося первоисточника."""

import re
from datetime import UTC
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.domain.event_facts import conflicting_facts, street_anchors, transport_routes
from relocant_media.services.source_revision_control import record_source_revision_decision
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, SourceRevision
from relocant_media.storage.revision_control import (
    SourceRevisionDecision,
    SourceRevisionMaterialization,
)

_WORD = re.compile(
    r"\b(?:otkaz\w*|odlož\w*|odloz\w*|pomer\w*|promen\w*|premešt\w*|"
    r"отказ\w*|одлож\w*|помер\w*|промен\w*|премешт\w*)\b", re.I,
)
_HOUR = re.compile(r"\b(?:u|у)\s+\d{1,2}(?:[:.]\d{2}|\s+(?:časova|sati|часова|сати))\b", re.I)
_CATEGORIES = {"transport", "utilities", "events"}


def meaningful_change(before: RawItem, after: CollectedRawItem) -> bool:
    left, right = (f"{x.title}\n{x.body}" for x in (before, after))
    if " ".join(left.split()) == " ".join(right.split()):
        return False
    published = before.published_at or after.published_at
    basis = published.year if published else 2000
    return (conflicting_facts(left, right, basis, basis)
            or street_anchors(left) != street_anchors(right)
            or transport_routes(left) != transport_routes(right)
            or set(_HOUR.findall(left)) != set(_HOUR.findall(right))
            or bool(_WORD.search(right)))


def queue_watched_change(
    session: Session, original: RawItem, live: CollectedRawItem,
) -> int | None:
    if (original.source_id, original.url) != (live.source_id, live.url):
        return None
    source = session.get(Source, original.source_id)
    run = session.get(EditorialRun, original.id)
    post = session.get(Post, run.post_id) if run and run.post_id else None
    if (source is None or source.ownership != "official" or not source.enabled
            or run is None or run.delivery_status != "sent" or post is None
            or post.category not in _CATEGORIES or not meaningful_change(original, live)):
        return None
    # All watches of a version share the original immutable receipt.
    lineage = session.scalar(select(SourceRevisionMaterialization).where(
        SourceRevisionMaterialization.raw_item_id == original.id,
    ))
    root = lineage.parent_raw_item_id if lineage is not None else original.id
    revision = session.scalar(select(SourceRevision).where(
        SourceRevision.raw_item_id == root,
    ).order_by(SourceRevision.observed_at.desc(), SourceRevision.id.desc()).limit(1))
    if revision is None:
        return None
    evidence: dict[str, Any] = revision.evidence
    published = live.published_at.astimezone(UTC).isoformat() if live.published_at else None
    if any(evidence.get(field) != getattr(live, field)
           for field in ("source_id", "url", "title", "body", "published_at_precision")) or (
        evidence.get("published_at") != published
    ):
        return None
    if session.scalar(select(SourceRevisionDecision.id).where(
        SourceRevisionDecision.revision_id == revision.id,
    ).limit(1)) is not None:
        return None
    record_source_revision_decision(
        session, revision_id=revision.id, decision="queue_reverify",
        reason_code="official_schedule_or_scope_changed", actor="editorial_source_watch",
        idempotency_key=f"watched-revision:{revision.id}", expected_decision_count=0,
    )
    version = session.get(SourceRevisionMaterialization, revision.id)
    if version is None:
        return None
    return version.raw_item_id
