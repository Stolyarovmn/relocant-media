"""Guarded reassessment of unresolved editorial decisions without a draft."""
import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.domain.audience import local_flight_change, local_utility_notice
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import event_key
from relocant_media.domain.freshness import evidence_freshness
from relocant_media.services.editorial import _candidate, _document, _event_items, _snapshot_member
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


def eligible_map_hold(
    session: Session, raw_item_id: int, *, now: datetime | None = None,
) -> bool:
    """Только неизданный точный пакет, для которого теперь появилась проверенная карта."""
    from relocant_media.services.editorial_products import build_presentation

    run = session.get(EditorialRun, raw_item_id, populate_existing=True)
    item = session.get(RawItem, raw_item_id, populate_existing=True)
    source = session.get(Source, item.source_id, populate_existing=True) if item else None
    if (run is None or item is None or source is None or not source.enabled
            or run.delivery_status != "pending" or run.staging_message_id is not None
            or run.outcome.get("status") != "human_review"
            or run.outcome.get("reason") != "event_place_requires_verification"
            or any(key in run.outcome for key in ("_rich_delivery", "_media_delivery"))):
        return False
    try:
        payload = run.outcome["_package"]
        package = EditorialPackage.model_validate_json(canonical_json(payload))
        post = session.get(Post, run.post_id, populate_existing=True) if run.post_id else None
        members = _event_items(session, anchor=item, anchor_source=source)
        snapshots = [_snapshot_member(member, policy) for member, policy in members]
        if (package.version != 2 or package.event is None or package.presentation is None
                or package.presentation.event_place is not None
                or package.presentation.native_map is not None
                or package.raw_item_id != item.id or package.source_id != item.source_id
                or package.city != source.city
                or run.outcome.get("_package_sha256") != hashlib.sha256(
                    canonical_json(payload).encode()).hexdigest()
                or post is None or post.published_at is not None
                or post.telegram_message_id is not None
                or (post.title, post.body) != (
                    package.outcome.draft.title, package.outcome.draft.body)
                or package.event.event_key != event_key(_candidate(item))
                or [d.model_dump(mode="json") for d in package.event.documents] != snapshots
                or run.evidence.get("documents") != snapshots):
            return False
        if evidence_freshness(
            item.title, item.body, item.published_at, now or datetime.now(UTC),
            precision=item.published_at_precision, source_type=source.source_type,
            verified_at=item.ingested_at,
        ).action != "keep":
            return False
        presentation = build_presentation(
            session, item.id, tuple(package.event.documents),
            document_ids=[member.id for member, _ in members],
            profile=load_city_profile(source.city),
            rubric_id=package.presentation.rubric_id,
        )
        return presentation.event_place is not None
    except (ValueError, TypeError, KeyError):
        return False


def eligible_policy_reassessment(
    session: Session, raw_item_id: int, *, now: datetime | None = None,
) -> bool:
    return (eligible_empty_policy_hold(session, raw_item_id, now=now)
            or eligible_map_hold(session, raw_item_id, now=now))


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
            or run.staging_message_id is not None
            or any(outcome.get(key) is not None for key in ("claims", "verification", "draft"))
            or "_package" in outcome):
        return False
    status, reason = outcome.get("status"), outcome.get("reason")
    rejected = (status, reason) == ("skipped", "ranking_rejected")
    utility = rejected and local_utility_notice(item.source_id, item.url, item.title, item.body)
    if "_autonomous_assessment" in outcome:
        previous = outcome.get("_previous_policy_hold", {}).get("outcome", {})
        if not utility or previous.get("reason") != "selection_ungrounded_evidence":
            return False
    if not rejected and (status != "human_review" or reason not in {
        "source_auto_publish_disabled", "selection_ungrounded_evidence",
    }):
        return False
    if evidence_freshness(item.title, item.body, item.published_at, now or datetime.now(UTC),
                          precision=item.published_at_precision, source_type=source.source_type,
                          verified_at=item.ingested_at).action != "keep":
        return False
    if rejected and not utility:
        profile = load_city_profile(source.city)
        if not local_flight_change(item.title, item.body, profile.geography.city_aliases):
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
        if not eligible_policy_reassessment(session, raw_id, now=now):
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
