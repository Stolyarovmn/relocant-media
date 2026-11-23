"""Import independently reviewed packages without AI clients or Telegram effects."""

import hashlib
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.models import PublicationStatus
from relocant_media.ai.package import EditorialPackage, canonical_json, evaluate_package
from relocant_media.domain.audience import READER_VALUE_POLICY_VERSION
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
from relocant_media.services.flight_review import flight_review_reference
from relocant_media.services.material_lifecycle import (
    PUBLISHED_TRANSPORT_DUPLICATE,
    LifecycleBlockedError,
    confirm_transport_duplicate,
    ensure_not_terminal,
)
from relocant_media.services.policy_reassessment import (
    eligible_empty_policy_hold,
    eligible_map_hold,
    eligible_reader_category_hold,
    policy_hold_audit,
)
from relocant_media.services.reader_reports import is_reader_report_source, submission_id_for
from relocant_media.storage.locks import lock_editorial_city as _lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


class PackageConflictError(ValueError):
    pass


def import_package(
    session: Session, package: EditorialPackage, *, now: datetime | None = None,
    replace_empty_hold: bool = False, replace_map_hold: bool = False,
    replace_flight_hold: bool = False,
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
            item.published_at_precision != package.evidence.published_at_precision
            or _timestamp(item.published_at) != _timestamp(package.evidence.published_at)
        ):
            raise PackageConflictError("raw evidence changed")
        existing = session.get(EditorialRun, item.id)
        if existing is None or (
            existing.delivery_status != "sent"
            and existing.outcome.get("reason") != PUBLISHED_TRANSPORT_DUPLICATE
        ):
            try:
                ensure_not_terminal(session, item.id, now=now or datetime.now(UTC))
            except LifecycleBlockedError as error:
                raise PackageConflictError(str(error)) from error
        flight_parent = (flight_review_reference(session, existing, city=package.city,
                         now=now or datetime.now(UTC)) if replace_flight_hold else None)
        if flight_parent is not None and (package.version != 2 or package.linked_update is None
                                           or package.linked_update.parent != flight_parent):
            raise PackageConflictError("flight update does not match the held parent")
        replacing = bool(existing is not None and package.version == 2 and (
            (replace_empty_hold and eligible_empty_policy_hold(session, item.id, now=now))
            or (replace_map_hold and (eligible_map_hold(session, item.id, now=now)
                                     or eligible_reader_category_hold(session, item.id, now=now)))
            or flight_parent is not None
        ))
        if existing is not None and not replacing:
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
        from relocant_media.services.linked_updates import validate_linked_update

        validate_linked_update(session, package, now=now or datetime.now(UTC))
        reviewed = _reviewed_event_item(session, anchor=item, anchor_source=source)
        if reviewed is not None:
            raise PackageConflictError(f"event already reviewed via raw item {reviewed.id}")

        members = _event_items(session, anchor=item, anchor_source=source)
        if package.presentation is not None:
            from relocant_media.services.editorial_products import verify_stored_presentation

            assert package.event is not None
            verify_stored_presentation(
                session, item.id, tuple(package.event.documents),
                document_ids=[member.id for member, _ in members],
                presentation=package.presentation)
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
            reason = _auto_publish_policy_reason(
                members, anchor_source=source, category=outcome.ranking.category,
            )
            # Package v1 reviews bind only the primary document. Never treat
            # them as attestations covering additional event evidence.
            if reason is None and package.version == 1 and len(members) > 1:
                reason = "package_requires_event_bundle_review"
            if reason is not None:
                outcome = outcome.model_copy(update={
                    "status": PublicationStatus.HUMAN_REVIEW, "reason": reason,
                })
        duplicate = None
        if (outcome.status == PublicationStatus.READY and outcome.ranking.category == "transport"
                and package.linked_update is None):
            from relocant_media.services.editorial_products import (
                published_transport_duplicate,
                recent_publications,
            )
            from relocant_media.services.event_lifecycle import source_revision_lifecycle_context

            if source_revision_lifecycle_context(session, item.id) is None:
                checked_at = now or datetime.now(UTC)
                duplicate = published_transport_duplicate(
                    recent_publications(session, package.city, now=checked_at),
                    package.outcome.draft, now=checked_at,
                )
                if duplicate is not None:
                    outcome = outcome.model_copy(update={
                        "status": PublicationStatus.SKIPPED,
                        "reason": PUBLISHED_TRANSPORT_DUPLICATE,
                        "draft": None,
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
        saved_outcome = {**outcome.model_dump(mode="json"),
                         "_package": payload, "_package_sha256": fingerprint}
        if is_reader_report_source(source):
            submission_id = submission_id_for(item)
            if submission_id is None:
                raise PackageConflictError("reader report has no submission identity")
            saved_outcome["_reader_submission_id"] = submission_id
        if replacing:
            assert existing is not None
            saved_outcome["_previous_policy_hold"] = policy_hold_audit(existing)
            saved_outcome["_autonomous_assessment"] = {
                "reviewed_package": True, "reader_value_policy": READER_VALUE_POLICY_VERSION}
            run = existing
            run.evidence = evidence
            run.outcome = saved_outcome
            run.post_id = post.id if post else None
            run.editorial_rule_version = CURRENT_EDITORIAL_RULE_VERSION
        else:
            run = EditorialRun(
                raw_item_id=item.id, evidence=evidence, outcome=saved_outcome,
                post_id=post.id if post else None, delivery_status="pending",
                editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
            )
            session.add(run)
        if duplicate is not None:
            session.flush()
            if not confirm_transport_duplicate(
                session, item.id, duplicate, now=now or datetime.now(UTC),
            ):
                raise PackageConflictError("transport duplicate cannot be recorded safely")
        session.commit()
        return run
    except BaseException:
        session.rollback()
        raise
