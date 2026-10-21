"""Auditable control decisions for changed primary/secondary source snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from relocant_media.services.event_lifecycle import materialize_source_revision
from relocant_media.storage.models import RawItem, Source, SourceRevision
from relocant_media.storage.revision_control import (
    SourceRevisionDecision,
    SourceRevisionMaterialization,
)

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
    expected_decision_count: int,
    note: str | None = None,
    decided_at: datetime | None = None,
) -> SourceRevisionDecision:
    """Append one control decision only if the caller still has the current control snapshot."""
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
    if expected_decision_count < 0:
        raise ValueError("expected_decision_count cannot be negative")

    revision = session.scalar(
        select(SourceRevision)
        .where(SourceRevision.id == revision_id)
        .with_for_update()
    )
    if revision is None:
        raise ValueError("source revision does not exist")

    # Idempotent replay is checked before the compare-and-set precondition. A caller may
    # safely retry the exact operation after a timeout even though the successful first
    # attempt has already increased decision_count.
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
        if existing.decision == "queue_reverify":
            materialize_source_revision(session, revision_id)
        return existing

    current_decision_count = session.scalar(
        select(func.count())
        .select_from(SourceRevisionDecision)
        .where(SourceRevisionDecision.revision_id == revision_id)
    )
    current_count = int(current_decision_count or 0)
    if current_count != expected_decision_count:
        raise ValueError(
            "stale source revision control snapshot: "
            f"expected decision_count {expected_decision_count}, found {current_count}"
        )

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
    if normalized_decision == "queue_reverify":
        materialize_source_revision(session, revision_id)
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
    materializations: dict[int, SourceRevisionMaterialization] = {}
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
        materializations = {
            row.revision_id: row
            for row in session.scalars(
                select(SourceRevisionMaterialization).where(
                    SourceRevisionMaterialization.revision_id.in_(revision_ids)
                )
            )
        }

    items: list[dict[str, Any]] = []
    undecided = 0
    for revision, raw, source in rows:
        history = decisions_by_revision.get(revision.id, [])
        latest = history[-1] if history else None
        materialization = materializations.get(revision.id)
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
                "write_precondition": {"expected_decision_count": len(history)},
                "decision_does_not_clear_fresh_review": True,
                "materialized_raw_item_id": (
                    None if materialization is None else materialization.raw_item_id
                ),
                "fresh_review_candidate_created": materialization is not None,
            }
        )

    items.sort(key=lambda item: (str(item["observed_at"]), int(item["revision_id"])), reverse=True)
    items.sort(key=lambda item: item["decision_status"] != "undecided")
    visible = items[:limit]
    return {
        "version": 3,
        "city": city,
        "total_revisions": len(items),
        "undecided_revisions": undecided,
        "returned_revisions": len(visible),
        "allowed_decisions": sorted(ALLOWED_REVISION_DECISIONS),
        "items": visible,
        "safety": {
            "decision_clears_reverification": False,
            "fresh_editorial_review_required_after_source_change": True,
            "queue_reverify_reuses_old_editorial_run": False,
            "stale_control_writes_rejected": True,
        },
    }
