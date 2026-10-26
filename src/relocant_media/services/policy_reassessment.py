"""Guarded autonomous assessment of previously unexamined policy holds."""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.domain.dedup import event_key
from relocant_media.domain.freshness import freshness_decision
from relocant_media.services.editorial import _candidate, _document, _event_items, _snapshot_member
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, RawItem, Source


def eligible_empty_policy_hold(
    session: Session, raw_item_id: int, *, now: datetime | None = None,
) -> bool:
    run = session.get(EditorialRun, raw_item_id, populate_existing=True)
    item = session.get(RawItem, raw_item_id, populate_existing=True)
    source = session.get(Source, item.source_id, populate_existing=True) if item else None
    if run is None or item is None or source is None or not source.enabled:
        return False
    outcome = run.outcome
    if (run.post_id is not None or run.delivery_status != "pending"
            or run.staging_message_id is not None or outcome.get("status") != "human_review"
            or outcome.get("reason") != "source_auto_publish_disabled"
            or any(outcome.get(key) is not None for key in ("claims", "verification", "draft"))
            or "_package" in outcome or "_autonomous_assessment" in outcome):
        return False
    if freshness_decision(item.title, item.body, item.published_at,
                          now or datetime.now(UTC),
                          published_at_precision=item.published_at_precision).action != "keep":
        return False
    members = _event_items(session, anchor=item, anchor_source=source)
    expected = [_snapshot_member(member, policy) for member, policy in members]
    return (run.evidence.get("city") == source.city
            and run.evidence.get("event_key") == event_key(_candidate(item))
            and run.evidence.get("documents") == expected
            and all(run.evidence.get(key) == value for key, value in
                    _document(item).model_dump(mode="json").items()))


def policy_hold_audit(run: EditorialRun) -> dict[str, Any]:
    return {"outcome": run.outcome, "evidence": run.evidence,
            "created_at": run.created_at.isoformat()}


def persist_policy_reassessment(
    session: Session, raw_id: int, outcome: EditorialOutcome,
    documents: tuple[EvidenceDocument, ...], *, now: datetime | None = None,
) -> EditorialRun:
    """Resolve a hold without changing evidence, receipts or granting publication."""
    now = now or datetime.now(UTC)
    try:
        item = session.get(RawItem, raw_id)
        source = session.get(Source, item.source_id) if item else None
        if source is None:
            raise ValueError("policy hold source missing")
        lock_editorial_city(session, source.city)
        if not eligible_empty_policy_hold(session, raw_id, now=now):
            raise ValueError("policy hold is not eligible for reassessment")
        assert item is not None
        members = _event_items(session, anchor=item, anchor_source=source)
        if documents != tuple(_document(member) for member, _ in members):
            raise ValueError("policy hold evidence changed")
        if outcome.status == PublicationStatus.READY:
            raise ValueError("ready reassessment requires independently reviewed package")
        run = session.get(EditorialRun, raw_id)
        assert run is not None
        previous = policy_hold_audit(run)
        run.outcome = {**outcome.model_dump(mode="json"),
                       "_previous_policy_hold": previous,
                       "_autonomous_assessment": {"checked_at": now.isoformat()}}
        session.commit()
        return run
    except BaseException:
        session.rollback()
        raise
