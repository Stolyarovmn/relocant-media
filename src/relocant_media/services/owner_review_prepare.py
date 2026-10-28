"""Upgrade verified owner-decidable policy holds into independently reviewed owner packages."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.ai.gemini_process import GeminiWorkerError
from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.package import EditorialPackage, canonical_json, evaluate_package
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.domain.dedup import event_key
from relocant_media.domain.freshness import evidence_freshness
from relocant_media.domain.originality import copies_source_text
from relocant_media.domain.policy import OWNER_DECISION_REASONS, primary_evidence_kind
from relocant_media.services.editorial import (
    DeliveryBlockedError,
    _candidate,
    _event_items,
    _snapshot_member,
)
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

# Two fresh approvals leave a low-risk package READY by content; the source/category hold
# keeps it HUMAN_REVIEW, so it is stored as HUMAN_REVIEW and only the owner can release it.
REVIEWED_STATES = frozenset({
    (PublicationStatus.HUMAN_REVIEW, "package_requires_review"),
    (PublicationStatus.READY, "package_independently_reviewed"),
})


def assert_primary_evidence(
    ownerships: list[str], category: str, draft: Any, source_texts: list[str],
) -> str:
    """Official evidence, or verified media outside high-risk topics with our own retelling."""
    kind = primary_evidence_kind(ownerships, category)
    if kind is None:
        raise DeliveryBlockedError("official primary evidence is required")
    if kind == "media" and copies_source_text(f"{draft.title}\n{draft.body}", source_texts):
        raise DeliveryBlockedError("draft copies the media source text")
    return kind


def _package_sha256(package: EditorialPackage) -> str:
    payload = canonical_json(package.model_dump(mode="json")).encode()
    return hashlib.sha256(payload).hexdigest()


def pending_owner_decision_holds(engine: Engine, *, now: datetime | None = None) -> list[int]:
    """Return fresh drafted outcomes the editorial could not decide, still unreviewed."""
    checked_at = now if now is not None else datetime.now(UTC)
    with Session(engine) as session:
        rows = session.execute(
            select(EditorialRun, Post, RawItem, Source.source_type)
            .join(Post, EditorialRun.post_id == Post.id)
            .join(RawItem, EditorialRun.raw_item_id == RawItem.id)
            .join(Source, RawItem.source_id == Source.id)
            .where(
                EditorialRun.delivery_status == "pending",
                Post.status == PublicationStatus.HUMAN_REVIEW.value,
            )
            .order_by(EditorialRun.raw_item_id)
        ).all()
        return [
            run.raw_item_id
            for run, post, item, source_type in rows
            if "_package" not in run.outcome
            and run.outcome.get("reason") in OWNER_DECISION_REASONS
            and isinstance(run.outcome.get("draft"), dict)
            and post.telegram_message_id is None
            and post.published_at is None
            and evidence_freshness(
                item.title, item.body, item.published_at, checked_at,
                precision=item.published_at_precision, source_type=source_type,
                verified_at=item.ingested_at,
            ).action == "keep"
        ]


def _candidate_rows(
    session: Session,
    raw_item_id: int,
) -> tuple[EditorialRun, Post, RawItem, Source]:
    run = session.get(EditorialRun, raw_item_id, populate_existing=True)
    item = session.get(RawItem, raw_item_id, populate_existing=True)
    source = session.get(Source, item.source_id, populate_existing=True) if item else None
    post = session.get(Post, run.post_id, populate_existing=True) if run and run.post_id else None
    if run is None or item is None or source is None or post is None or not source.enabled:
        raise DeliveryBlockedError("high-risk owner-review candidate is incomplete")
    return run, post, item, source


def _build_package(session: Session, raw_item_id: int) -> EditorialPackage:
    run, post, item, source = _candidate_rows(session, raw_item_id)
    if (
        run.delivery_status != "pending"
        or run.staging_message_id is not None
        or post.telegram_message_id is not None
        or post.published_at is not None
        or post.status != PublicationStatus.HUMAN_REVIEW.value
    ):
        raise DeliveryBlockedError("high-risk owner-review candidate has a delivery receipt")
    outcome = EditorialOutcome.model_validate(run.outcome)
    if (
        outcome.status != PublicationStatus.HUMAN_REVIEW
        or outcome.reason not in OWNER_DECISION_REASONS
        or outcome.draft is None
    ):
        raise DeliveryBlockedError("candidate is not an owner-decidable hold")
    if (post.title, post.body) != (outcome.draft.title, outcome.draft.body):
        raise DeliveryBlockedError("stored high-risk draft changed before owner review")
    lock_editorial_city(session, source.city)
    members = _event_items(session, anchor=item, anchor_source=source)
    snapshots = [_snapshot_member(member, policy) for member, policy in members]
    frozen = run.evidence.get("documents")
    if not isinstance(frozen, list) or frozen != snapshots:
        raise DeliveryBlockedError("high-risk evidence bundle changed before owner review")
    assert_primary_evidence(
        [policy.ownership for _, policy in members], outcome.ranking.category, outcome.draft,
        [f"{member.title}\n{member.body}" for member, _ in members],
    )
    primary = EvidenceDocument.model_validate(run.evidence)
    normalized = outcome.model_copy(
        update={
            # Package evaluation immediately converts this asserted READY state back to
            # HUMAN_REVIEW because the category remains high-risk. The normalization is
            # only an input to the same two-review package contract used by safe news.
            "status": PublicationStatus.READY,
            "reason": "owner_review_candidate",
        }
    )
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
    payload = {
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
    }
    return EditorialPackage.model_validate_json(json.dumps(payload))


def _persist_reviewed_package(
    session: Session,
    raw_item_id: int,
    package: EditorialPackage,
    outcome: EditorialOutcome,
) -> None:
    run, post, item, source = _candidate_rows(session, raw_item_id)
    if "_package" in run.outcome or run.delivery_status != "pending":
        raise DeliveryBlockedError("high-risk candidate changed during review")
    lock_editorial_city(session, source.city)
    members = _event_items(session, anchor=item, anchor_source=source)
    current_snapshots = [_snapshot_member(member, policy) for member, policy in members]
    assert package.event is not None
    package_snapshots = [
        document.model_dump(mode="json") for document in package.event.documents
    ]
    if (
        package.event.event_key != event_key(_candidate(item))
        or package_snapshots != current_snapshots
    ):
        raise DeliveryBlockedError("evidence changed during independent review")
    metadata = {
        key: value for key, value in run.outcome.items() if key.startswith("_")
    }
    run.outcome = {
        **outcome.model_dump(mode="json"),
        **metadata,
        "_package": package.model_dump(mode="json"),
        "_package_sha256": _package_sha256(package),
    }
    post.category = package.outcome.ranking.category
    post.status = PublicationStatus.HUMAN_REVIEW.value
    post.title = package.outcome.draft.title
    post.body = package.outcome.draft.body
    post.confidence = Decimal(str(outcome.confidence))
    session.commit()


_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
_DETAIL = re.compile(r"[A-Za-z0-9_.:() -]{1,100}")


def summarize_preparation_audit(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per blocked candidate: error type and each review's verdict, score, flags and rubric.

    Review text, reasons and hashes are never copied; at most five candidates are kept.
    """
    details: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for entry in entries:
        role = entry.get("role")
        if role in ("facts", "utility"):
            review: dict[str, Any] = {"role": role}
            if isinstance(entry.get("verdict"), str) and _TOKEN.fullmatch(entry["verdict"]):
                review["verdict"] = entry["verdict"]
            score = entry.get("computed_score", entry.get("score"))
            if type(score) is int:
                review["score"] = score
            flags = entry.get("flags")
            review["flags"] = [flag for flag in (flags if isinstance(flags, list) else [])[:5]
                               if isinstance(flag, str) and _TOKEN.fullmatch(flag)]
            rubric = entry.get("rubric")
            if isinstance(rubric, dict):
                review["rubric"] = {key: value for key, value in rubric.items()
                                    if isinstance(key, str) and _TOKEN.fullmatch(key)
                                    and type(value) is int}
            pending.append(review)
        elif role == "owner_review_prepare" and entry.get("status") == "blocked":
            raw_item_id = entry.get("raw_item_id")
            error_type = entry.get("error_type")
            if type(raw_item_id) is int and isinstance(error_type, str):
                detail = entry.get("error_detail")
                item: dict[str, Any] = {"raw_item_id": raw_item_id,
                                        "error_type": error_type[:40], "reviews": pending[-3:]}
                if isinstance(detail, str) and _DETAIL.fullmatch(detail):
                    item["error_detail"] = detail
                details.append(item)
            pending = []
    return details[:5]


def _live_fetcher(
    package: EditorialPackage,
    fetcher: Callable[[EditorialPackage], tuple[EvidenceDocument, ...]],
) -> Callable[[], tuple[EvidenceDocument, ...]]:
    def live() -> tuple[EvidenceDocument, ...]:
        return fetcher(package)

    return live


def prepare_owner_decision_holds(
    engine: Engine,
    provider: StructuredAIProvider,
    live_documents: Callable[[EditorialPackage], tuple[EvidenceDocument, ...]],
    *,
    limit: int = 5,
    now: datetime | None = None,
    audit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    """Attach two fresh independent reviews while preserving final HUMAN_REVIEW status."""
    prepared = 0
    blocked = 0
    for raw_item_id in pending_owner_decision_holds(engine, now=now)[:limit]:
        try:
            with Session(engine) as session:
                package = _build_package(session, raw_item_id)
            package = review_with_repairs(
                package,
                provider,
                _live_fetcher(package, live_documents),
                audit=audit,
            )
            evaluated = evaluate_package(package)
            if (
                (evaluated.status, evaluated.reason) not in REVIEWED_STATES
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
            with Session(engine) as session:
                _persist_reviewed_package(session, raw_item_id, package, evaluated.model_copy(
                    update={"status": PublicationStatus.HUMAN_REVIEW,
                            "reason": "package_requires_review"}))
            prepared += 1
        except Exception as error:
            blocked += 1
            if audit is not None:
                audit(
                    {
                        "role": "owner_review_prepare",
                        "raw_item_id": raw_item_id,
                        "status": "blocked",
                        "error_type": type(error).__name__,
                        # Fixed texts of our own worker errors only (class names, no values).
                        **({"error_detail": str(error)[:100]}
                           if isinstance(error, GeminiWorkerError) else {}),
                    }
                )
    return {"prepared": prepared, "blocked": blocked}
