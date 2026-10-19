"""Import independently reviewed packages without AI clients or Telegram effects."""

import hashlib
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.models import PublicationStatus
from relocant_media.ai.package import EditorialPackage, canonical_json, evaluate_package
from relocant_media.services.editorial import _timestamp
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
        item = session.scalar(select(RawItem).where(
            RawItem.id == package.raw_item_id,
        ).with_for_update())
        source = session.get(Source, package.source_id)
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
            if (existing.outcome.get("_package_sha256") != fingerprint
                    or existing.evidence != evidence):
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
        post = None
        if outcome.status in {PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW}:
            post = Post(
                city=package.city, category=outcome.ranking.category,
                status=outcome.status.value, title=package.outcome.draft.title,
                body=package.outcome.draft.body, confidence=Decimal(str(outcome.confidence)),
            )
            session.add(post)
            session.flush()
        run = EditorialRun(
            raw_item_id=item.id, evidence=evidence,
            outcome={**outcome.model_dump(mode="json"),
                     "_package": payload, "_package_sha256": fingerprint},
            post_id=post.id if post else None, delivery_status="pending",
        )
        session.add(run)
        session.commit()
        return run
    except BaseException:
        session.rollback()
        raise
