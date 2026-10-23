"""Upgrade verified high-risk policy holds into independently reviewed owner packages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.package import EditorialPackage, canonical_json, evaluate_package
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.domain.dedup import event_key
from relocant_media.domain.policy import HIGH_RISK_CATEGORIES
from relocant_media.services.editorial import (
    DeliveryBlockedError,
    _candidate,
    _event_items,
    _snapshot_member,
)
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


def _package_sha256(package: EditorialPackage) -> str:
    return hashlib.sha256(canonical_json(package.model_dump(mode="json")).encode()).hexdigest()


def pending_high_risk_holds(engine: Engine) -> list[int]:
    """Return only full AI outcomes held solely by the high-risk human-review policy."""
    with Session(engine) as session:
        rows = session.execute(
            select(EditorialRun, Post)
            .join(Post, EditorialRun.post_id == Post.id)
            .where(
                EditorialRun.delivery_status == "pending",
                Post.status == PublicationStatus.HUMAN_REVIEW.value,
                Post.category.in_(tuple(HIGH_RISK_CATEGORIES)),
            )
            .order_by(EditorialRun.raw_item_id)
        ).all()
        return [
            run.raw_item_id
            for run, post in rows
            if "_package" not in run.outcome
            and run.outcome.get("reason") == "policy_requires_human_review"
            and isinstance(run.outcome.get("draft"), dict)
            and post.telegram_message_id is None
            and post.published_at is None
        ]


def _build_package(session: Session, raw_item_id: int) -> EditorialPackage:
    run = session.get(EditorialRun, raw_item_id, populate_existing=True)
    item = session.get(RawItem, raw_item_id, populate_existing=True)
    source = session.get(Source, item.source_id, populate_existing=True) if item else None
    post = session.get(Post, run.post_id, populate_existing=True) if run and run.post_id else None
    if run is None or item is None or source is None or post is None or not source.enabled:
        raise DeliveryBlockedError("high-risk owner-review candidate is incomplete")
    if (
        run.delivery_status != "pending"
        or run.staging_message_id is not None
        or post.telegram_message_id is not None
        or post.published_at is not None
        or post.status != PublicationStatus.HUMAN_REVIEW.value
    ):
        raise DeliveryBlockedError("high-risk owner-review candidate has a delivery receipt")
    outcome = EditorialOutcome.model_validate(run.outcome)
    category = outcome.ranking.category.strip().lower()
    if (
        category not in HIGH_RISK_CATEGORIES
        or outcome.status != PublicationStatus.HUMAN_REVIEW
        or outcome.reason != "policy_requires_human_review"
        or outcome.draft is None
    ):
        raise DeliveryBlockedError("candidate is not a pure high-risk policy hold")
    if (post.title, post.body) != (outcome.draft.title, outcome.draft.body):
        raise DeliveryBlockedError("stored high-risk draft changed before owner review")
    lock_editorial_city(session, source.city)
    members = _event_items(session, anchor=item, anchor_source=source)
    snapshots = [_snapshot_member(member, policy) for member, policy in members]
    frozen = run.evidence.get("documents")
    if not isinstance(frozen, list) or frozen != snapshots:
        raise DeliveryBlockedError("high-risk evidence bundle changed before owner review")
    if not any(
        policy.ownership.strip().casefold() == "official" for _, policy in members
    ):
        raise DeliveryBlockedError("official primary evidence is required")
    primary = EvidenceDocument.model_validate(run.evidence)
    normalized = outcome.model_copy(update={
        # Package evaluation immediately converts this asserted READY state back to
        # HUMAN_REVIEW for high-risk categories. This normalization exists only so the
        # package contract can run the same two-review machinery used by auto-safe news.
        "status": PublicationStatus.READY,
        "reason": "owner_review_candidate",
    })
    placeholders = [
        {
            "reviewer_id": f"pending-{role}",
            "role": role,
            "source_id": item.source_id,
            "article_id": item.external_id,
            "context_sha256": "0" * 64,
            "draft_sha256": "0" * 64,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "verdict": "hold",
            "score": 0,
            "flags": ["pending_review"],
            "live_source_checked": False,
        }
        for role in ("facts", "utility")
    ]
    return EditorialPackage.model_validate_json(json.dumps({
        "version": 2,
        "policy_version": "subscription-editorial-v2",
        "city": source.city,
        "raw_item_id": item.id,
        "source_id": item.source_id,
        "article_id": item.external_id,
        "urgent": False,
        "evidence": primary.model_dump(mode="json"),
        "event": {
            "event_key": event_key(_candidate(item)),
            "editorial_rule_version": "belgrade-editorial-v2",
            "documents": snapshots,
        },
        "outcome": normalized.model_dump(mode="json"),
        "reviews": placeholders,
    }))


def prepare_high_risk_holds(
    engine: Engine,
    provider: StructuredAIProvider,
    live_documents: Callable[[EditorialPackage], tuple[EvidenceDocument, ...]],
    *,
    limit: int = 5,
    audit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Attach two fresh independent reviews, while leaving final status HUMAN_REVIEW."""
    prepared = 0
    blocked = 0
    for raw_item_id in pending_high_risk_holds(engine)[:limit]:
        try:
            with Session(engine) as session:
                package = _build_package(session, raw_item_id)
            package = review_with_repairs(
                package,
                provider,
                lambda current=package: live_documents(current),
                audit=audit,
            )
            evaluated = evaluate_package(package)
            if (
                evaluated.status != PublicationStatus.HUMAN_REVIEW
                or evaluated.reason != "package_requires_review"
                or evaluated.draft is None
                or any(
                    review.verdict != "approve"
                    or review.score < 75
                    or review.flags
                    or not review.live_source_checked
                    for review in package.reviews
                )
            ):
                raise DeliveryBlockedError("high-risk package failed independent review")
            fingerprint = _package_sha256(package)
            with Session(engine) as session:
                run = session.get(EditorialRun, raw_item_id, populate_existing=True)
                item = session.get(RawItem, raw_item_id, populate_existing=True)
                source = session.get(Source, item.source_id, populate_existing=True) if item else None
                post = session.get(Post, run.post_id, populate_existing=True) if run and run.post_id else None
                if run is None or item is None or source is None or post is None:
                    raise DeliveryBlockedError("high-risk candidate disappeared")
                if "_package" in run.outcome or run.delivery_status != "pending":
                    raise DeliveryBlockedError("high-risk candidate changed during review")
                lock_editorial_city(session, source.city)
                members = _event_items(session, anchor=item, anchor_source=source)
                current_snapshots = [_snapshot_member(member, policy) for member, policy in members]
                assert package.event is not None
                if (
                    package.event.event_key != event_key(_candidate(item))
                    or [document.model_dump(mode="json") for document in package.event.documents]
                    != current_snapshots
                ):
                    raise DeliveryBlockedError("evidence changed during independent review")
                metadata = {key: value for key, value in run.outcome.items() if key.startswith("_")}
                run.outcome = {
                    **evaluated.model_dump(mode="json"),
                    **metadata,
                    "_package": package.model_dump(mode="json"),
                    "_package_sha256": fingerprint,
                }
                post.category = package.outcome.ranking.category
                post.status = PublicationStatus.HUMAN_REVIEW.value
                post.title = package.outcome.draft.title
                post.body = package.outcome.draft.body
                post.confidence = Decimal(str(evaluated.confidence))
                session.commit()
            prepared += 1
        except Exception as error:
            blocked += 1
            if audit is not None:
                audit({
                    "role": "owner_review_prepare",
                    "raw_item_id": raw_item_id,
                    "status": "blocked",
                    "error_type": type(error).__name__,
                })
    return {"prepared": prepared, "blocked": blocked}
