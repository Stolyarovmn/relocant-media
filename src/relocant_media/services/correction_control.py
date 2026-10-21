"""Auditable editorial decisions for reader-submitted corrections."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from relocant_media.storage.correction_control import ReaderCorrectionDecision
from relocant_media.storage.models import Submission

ALLOWED_CORRECTION_DECISIONS = frozenset(
    {"queue_verify", "hold", "confirmed", "not_supported", "superseded"}
)
_RESOLVED_DECISIONS = frozenset({"confirmed", "not_supported", "superseded"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def record_reader_correction_decision(
    session: Session,
    *,
    submission_id: int,
    decision: str,
    reason_code: str,
    actor: str,
    idempotency_key: str,
    expected_decision_count: int,
    note: str | None = None,
    decided_at: datetime | None = None,
) -> ReaderCorrectionDecision:
    """Append a decision only if the caller still has the current control snapshot."""
    normalized_decision = decision.strip().casefold()
    normalized_reason = reason_code.strip()
    normalized_actor = actor.strip()
    normalized_key = idempotency_key.strip()
    if normalized_decision not in ALLOWED_CORRECTION_DECISIONS:
        raise ValueError("unsupported reader correction decision")
    if not normalized_reason or not normalized_actor or not normalized_key:
        raise ValueError("reason_code, actor and idempotency_key are required")
    if len(normalized_key) > 128:
        raise ValueError("idempotency_key is too long")
    if expected_decision_count < 0:
        raise ValueError("expected_decision_count cannot be negative")

    submission = session.scalar(
        select(Submission)
        .where(Submission.id == submission_id)
        .with_for_update()
    )
    if submission is None:
        raise ValueError("submission does not exist")
    if submission.kind != "correction":
        raise ValueError("submission is not a reader correction")
    if submission.payload.get("city") != "belgrade":
        raise ValueError("reader correction is outside the configured city")

    # Exact replay wins over the compare-and-set precondition. This keeps retries safe
    # after a timeout while rejecting a distinct decision based on stale control state.
    existing = session.scalar(
        select(ReaderCorrectionDecision).where(
            ReaderCorrectionDecision.idempotency_key == normalized_key
        )
    )
    if existing is not None:
        if (
            existing.submission_id != submission_id
            or existing.decision != normalized_decision
            or existing.reason_code != normalized_reason
            or existing.actor != normalized_actor
            or existing.note != note
        ):
            raise ValueError("idempotency_key already belongs to another correction decision")
        return existing

    current_decision_count = session.scalar(
        select(func.count())
        .select_from(ReaderCorrectionDecision)
        .where(ReaderCorrectionDecision.submission_id == submission_id)
    )
    current_count = int(current_decision_count or 0)
    if current_count != expected_decision_count:
        raise ValueError(
            "stale reader correction control snapshot: "
            f"expected decision_count {expected_decision_count}, found {current_count}"
        )

    row = ReaderCorrectionDecision(
        submission_id=submission_id,
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


def build_reader_correction_control(
    session: Session,
    *,
    city: str = "belgrade",
    limit: int = 100,
) -> dict[str, Any]:
    """Return privacy-safe correction status without exposing private correction prose."""
    if limit < 1:
        raise ValueError("limit must be positive")

    submissions = list(
        session.scalars(
            select(Submission)
            .where(
                Submission.kind == "correction",
                Submission.payload["city"].as_string() == city,
            )
            .order_by(Submission.created_at.desc(), Submission.id.desc())
        )
    )
    ids = [row.id for row in submissions]
    decisions_by_submission: dict[int, list[ReaderCorrectionDecision]] = {}
    if ids:
        for decision in session.scalars(
            select(ReaderCorrectionDecision)
            .where(ReaderCorrectionDecision.submission_id.in_(ids))
            .order_by(
                ReaderCorrectionDecision.submission_id,
                ReaderCorrectionDecision.decided_at,
                ReaderCorrectionDecision.id,
            )
        ):
            decisions_by_submission.setdefault(decision.submission_id, []).append(decision)

    items: list[dict[str, Any]] = []
    unresolved = 0
    for submission in submissions:
        history = decisions_by_submission.get(submission.id, [])
        latest = history[-1] if history else None
        effective_decision = latest.decision if latest is not None else "unreviewed"
        is_resolved = effective_decision in _RESOLVED_DECISIONS
        if not is_resolved:
            unresolved += 1
        raw_ids = submission.payload.get("raw_item_ids", [])
        linked_raw_ids = (
            [value for value in raw_ids if type(value) is int]
            if isinstance(raw_ids, list)
            else []
        )
        items.append(
            {
                "submission_id": submission.id,
                "intake_status": submission.status,
                "created_at": _utc(submission.created_at).isoformat(),
                "linked_raw_item_ids": linked_raw_ids,
                "decision_status": "resolved" if is_resolved else "requires_attention",
                "effective_decision": effective_decision,
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
                "private_text_exposed": False,
                "telegram_mutation_performed": False,
            }
        )

    items.sort(
        key=lambda item: (str(item["created_at"]), int(item["submission_id"])),
        reverse=True,
    )
    items.sort(key=lambda item: item["decision_status"] == "resolved")
    visible = items[:limit]
    return {
        "version": 2,
        "city": city,
        "total_corrections": len(items),
        "unresolved_corrections": unresolved,
        "returned_corrections": len(visible),
        "allowed_decisions": sorted(ALLOWED_CORRECTION_DECISIONS),
        "items": visible,
        "safety": {
            "automatic_publication": False,
            "telegram_edit_capability": False,
            "private_submission_text_in_control_output": False,
            "confirmed_means_audited_editorial_conclusion_only": True,
            "stale_control_writes_rejected": True,
        },
    }
