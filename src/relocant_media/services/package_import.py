"""Import independently reviewed packages without AI clients or Telegram effects."""

import hashlib
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.models import PublicationStatus
from relocant_media.ai.package import EditorialPackage, canonical_json, evaluate_package
from relocant_media.domain.dedup import event_key
from relocant_media.domain.policy import CURRENT_EDITORIAL_RULE_VERSION
from relocant_media.services.editorial import (
    _auto_publish_policy_reason,
    _candidate,
    _event_items,
    _reviewed_event_item,
    _snapshot_member,
    _timestamp,
)
from relocant_media.storage.locks import lock_editorial_city as _lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


class PackageConflictError(ValueError):
    pass


def import_package(
    session: Session, package: EditorialPackage, *, now: datetime | None = None,
) -> EditorialRun:
    """Dedicated session required. Matching imports do not reset delivery receipts."""
    outcome = evaluate_package(package, now=now)
    payload = package.model_dump(mode="json")
    fingerprint = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    evidence = {**package.evidence.model_dump(mode="json"), "city": package.city}
    try:
        _lock_editorial_city(session, package.city)
        item = session.scalar(select(RawItem).where(
            RawItem.id == package.raw_item_id,
        ).with_for_update().execution_options(populate_existing=True))
        source = session.get(Source, package.source_id, populate_existing=True)
        if item is None or source is None or not source.enabled:
            raise PackageConflictError("raw article/source missing or disabled")
        if (item.source_id, item.external_id, source.city) != (
            package.source_id, package.article_id, package.city,
        ):
            raise PackageConflictError("source/article/city mismatch")
        if any(getattr(item, key) != getattr(package.evidence, key)
               for key in ("url", "title", "body")) or (
            _timestamp(item.published_at) != _timestamp(package.evidence.published_at)
        ):
            raise PackageConflictError("raw evidence changed")
        existing = session.get(EditorialRun, item.id)
        if existing is not None:
            # Both historical single-document runs and new bundles must keep
            # their original receipt and snapshot on an identical reimport.
            stored_primary = {key: existing.evidence.get(key) for key in evidence}
            if (existing.outcome.get("_package_sha256") != fingerprint
                    or stored_primary != evidence):
                raise PackageConflictError("a different editorial package already exists")
            post = session.get(Post, existing.post_id) if existing.post_id else None
            if existing.post_id is not None and post is None:
                raise PackageConflictError("stored draft is missing")
            if post is not None and (post.title, post.body) != (
                package.outcome.draft.title, package.outcome.draft.body,
            ):
                raise PackageConflictError("stored draft changed after package import")
            session.commit()
            return existing
        reviewed = _reviewed_event_item(session, anchor=item, anchor_source=source)
        if reviewed is not None:
            raise PackageConflictError(f"event already reviewed via raw item {reviewed.id}")

        members = _event_items(session, anchor=item, anchor_source=source)
        snapshots = [_snapshot_member(member, policy) for member, policy in members]
        key = event_key(_candidate(item))
        if package.version == 2:
            assert package.event is not None  # validated at the JSON boundary
            expected = {d.raw_item_id: d.model_dump(mode="json")
                        for d in package.event.documents}
            actual = {member.id: snapshot for (member, _), snapshot
                      in zip(members, snapshots, strict=True)}
            if package.event.event_key != key or expected != actual:
                raise PackageConflictError("event evidence or source policy changed")
        evidence = {
            **evidence,
            "event_key": key,
            "documents": snapshots,
        }
        if outcome.status == PublicationStatus.READY:
            reason = _auto_publish_policy_reason(members, anchor_source=source)
            # Package v1 reviews bind only the primary document. Never treat
            # them as attestations covering additional event evidence.
            if reason is None and package.version == 1 and len(members) > 1:
                reason = "package_requires_event_bundle_review"
            if reason is not None:
                outcome = outcome.model_copy(update={
                    "status": PublicationStatus.HUMAN_REVIEW, "reason": reason,
                })
        post = None
        if outcome.status in {PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW}:
            post = Post(
                city=package.city, category=outcome.ranking.category,
                status=outcome.status.value, title=package.outcome.draft.title,
                body=package.outcome.draft.body, confidence=Decimal(str(outcome.confidence)),
                editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
            )
            session.add(post)
            session.flush()
        run = EditorialRun(
            raw_item_id=item.id, evidence=evidence,
            outcome={**outcome.model_dump(mode="json"),
                     "_package": payload, "_package_sha256": fingerprint},
            post_id=post.id if post else None, delivery_status="pending",
            editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
        )
        session.add(run)
        session.commit()
        return run
    except BaseException:
        session.rollback()
        raise
