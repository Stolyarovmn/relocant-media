"""Auditable control decisions for changed primary/secondary source snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import RawItem, Source, SourceRevision
from relocant_media.storage.revision_control import SourceRevisionDecision

ALLOWED_REVISION_DECISIONS = frozenset({"queue_reverify", "hold", "superseded"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def record_source_revision_decision(
    session: Session,
    *,
    revision_id: int,
    decision: str,
    reason_code: str,
    actor: str,
    idempotency_key: str,
    note: str | None = None,
    decided_at: datetime | None = None,
) -> SourceRevisionDecision:
    """Append one control decision without changing editorial/delivery state."""
    normalized_decision = decision.strip().casefold()
    normalized_reason = reason_code.strip()
    normalized_actor = actor.strip()
    normalized_key = idempotency_key.strip()
    if normalized_decision not in ALLOWED_REVISION_DECISIONS:
        raise ValueError("unsupported source revision decision")
    if not normalized_reason or not normalized_actor or not normalized_key:
        raise ValueError("reason_code, actor and idempotency_key are required")
    if len(normalized_key) > 128:
        raise ValueError("idempotency_key is too long")
    if session.get(SourceRevision, revision_id) is None:
        raise ValueError("source revision does not exist")

    existing = session.scalar(
        select(SourceRevisionDecision).where(
            SourceRevisionDecision.idempotency_key == normalized_key
        )
    )
    if existing is not None:
        if (
            existing.revision_id != revision_id
            or existing.decision != normalized_decision
            or existing.reason_code != normalized_reason
            or existing.actor != normalized_actor
            or existing.note != note
        ):
            raise ValueError("idempotency_key already belongs to another decision")
        return existing

    row = SourceRevisionDecision(
        revision_id=revision_id,
        decision=normalized_decision,
        reason_code=normalized_reason,
        actor=normalized_actor,
        idempotency_key=normalized_key,
        note=note,
        decided_at=decided_at or datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def build_source_revision_control(
    session: Session,
    *,
    city: str = "belgrade",
    limit: int = 100,
) -> dict[str, Any]:
    """Project revision history plus latest decision into the main newsroom control surface."""
    if limit < 1:
        raise ValueError("limit must be positive")

    rows = session.execute(
        select(SourceRevision, RawItem, Source)
        .join(RawItem, RawItem.id == SourceRevision.raw_item_id)
        .join(Source, Source.id == RawItem.source_id)
        .where(Source.city == city)
        .order_by(SourceRevision.observed_at.desc(), SourceRevision.id.desc())
    ).all()
    revision_ids = [revision.id for revision, _raw, _source in rows]
    decisions_by_revision: dict[int, list[SourceRevisionDecision]] = {}
    if revision_ids:
        for decision in session.scalars(
            select(SourceRevisionDecision)
            .where(SourceRevisionDecision.revision_id.in_(revision_ids))
            .order_by(
                SourceRevisionDecision.revision_id,
                SourceRevisionDecision.decided_at,
                SourceRevisionDecision.id,
            )
        ):
            decisions_by_revision.setdefault(decision.revision_id, []).append(decision)

    items: list[dict[str, Any]] = []
    undecided = 0
    for revision, raw, source in rows:
        history = decisions_by_revision.get(revision.id, [])
        latest = history[-1] if history else None
        if latest is None:
            undecided += 1
        items.append(
            {
                "revision_id": revision.id,
                "raw_item_id": raw.id,
                "source_id": source.id,
                "source_name": source.name,
                "snapshot_sha256": revision.snapshot_sha256,
                "observed_at": _utc(revision.observed_at).isoformat(),
                "title": raw.title,
                "url": raw.url,
                "decision_status": "undecided" if latest is None else "recorded",
                "latest_decision": (
                    None
                    if latest is None
                    else {
                        "decision_id": latest.id,
                        "decision": latest.decision,
                        "reason_code": latest.reason_code,
                        "note": latest.note,
                        "actor": latest.actor,
                        "decided_at": _utc(latest.decided_at).isoformat(),
                    }
                ),
                "decision_count": len(history),
                "decision_does_not_clear_fresh_review": True,
            }
        )

    # Within each operational bucket show the newest revision first, then put undecided first.
    items.sort(key=lambda item: (str(item["observed_at"]), int(item["revision_id"])), reverse=True)
    items.sort(key=lambda item: item["decision_status"] != "undecided")
    visible = items[:limit]
    return {
        "version": 1,
        "city": city,
        "total_revisions": len(items),
        "undecided_revisions": undecided,
        "returned_revisions": len(visible),
        "allowed_decisions": sorted(ALLOWED_REVISION_DECISIONS),
        "items": visible,
        "safety": {
            "decision_clears_reverification": False,
            "fresh_editorial_review_required_after_source_change": True,
        },
    }
