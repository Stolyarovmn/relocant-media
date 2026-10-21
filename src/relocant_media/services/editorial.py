"""Persisted editorial processing and conservative, at-most-once staging delivery."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.publisher import Publisher
from relocant_media.domain.dedup import DEFAULT_EVENT_WINDOW, EventCandidate, event_key, same_event
from relocant_media.domain.policy import CURRENT_EDITORIAL_RULE_VERSION
from relocant_media.storage.locks import lock_editorial_city as _lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


class DeliveryBlockedError(ValueError):
    pass


def _timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _candidate(item: RawItem) -> EventCandidate:
    return EventCandidate(
        raw_item_id=item.id,
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        body=item.body,
        published_at=item.published_at,
        canonical_url=item.canonical_url,
        content_sha256=item.content_sha256,
    )


def _document(item: RawItem) -> EvidenceDocument:
    return EvidenceDocument(
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        body=item.body,
        published_at=item.published_at,
    )


def _reviewed_event_item(
    session: Session,
    *,
    anchor: RawItem,
    anchor_source: Source,
) -> RawItem | None:
    anchor_candidate = _candidate(anchor)
    exact_conditions = [
        RawItem.url == anchor.url,
        RawItem.canonical_url == anchor_candidate.resolved_canonical_url,
        RawItem.content_sha256 == anchor_candidate.resolved_content_sha256,
        # Legacy/directly imported rows may not have persisted fingerprints.
        # Include them for the exact same_event check rather than skipping them.
        RawItem.canonical_url.is_(None),
        RawItem.content_sha256.is_(None),
    ]

    match_scope = list(exact_conditions)
    if anchor.published_at is not None:
        match_scope.append(
            and_(
                RawItem.published_at >= anchor.published_at - DEFAULT_EVENT_WINDOW,
                RawItem.published_at <= anchor.published_at + DEFAULT_EVENT_WINDOW,
            )
        )

    reviewed = session.scalars(
        select(RawItem)
        .join(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .join(Source, Source.id == RawItem.source_id)
        .where(
            RawItem.id != anchor.id,
            Source.city == anchor_source.city,
            or_(*match_scope),
        )
        .order_by(RawItem.published_at, RawItem.id)
        .execution_options(populate_existing=True)
    )
    for item in reviewed:
        if same_event(anchor_candidate, _candidate(item)):
            return item
    return None


def _event_items(
    session: Session,
    *,
    anchor: RawItem,
    anchor_source: Source,
) -> list[tuple[RawItem, Source]]:
    """Return one evidence item per source for the same bounded event."""
    members: list[tuple[RawItem, Source]] = [(anchor, anchor_source)]
    if anchor.published_at is None:
        return members

    lower = anchor.published_at - DEFAULT_EVENT_WINDOW
    upper = anchor.published_at + DEFAULT_EVENT_WINDOW
    rows = session.execute(
        select(RawItem, Source)
        .join(Source, Source.id == RawItem.source_id)
        .where(
            RawItem.id != anchor.id,
            Source.city == anchor_source.city,
            Source.enabled.is_(True),
            RawItem.published_at >= lower,
            RawItem.published_at <= upper,
        )
        .order_by(RawItem.published_at, RawItem.id)
        .execution_options(populate_existing=True)
    ).all()

    anchor_candidate = _candidate(anchor)
    seen_sources = {anchor.source_id}
    for item, source in rows:
        if item.source_id in seen_sources:
            continue
        if not same_event(anchor_candidate, _candidate(item)):
            continue
        members.append((item, source))
        seen_sources.add(item.source_id)
    return members


def _auto_publish_policy_reason(
    members: list[tuple[RawItem, Source]],
    *,
    anchor_source: Source,
    category: str | None = None,
) -> str | None:
    if not anchor_source.auto_publish:
        return "source_auto_publish_disabled"
    if any(source.high_risk for _, source in members):
        return "source_policy_requires_review"
    if anchor_source.source_type == "official_venue_events" and category != "events":
        return "venue_events_only"
    if not any(source.ownership.strip().casefold() == "official" for _, source in members):
        return "official_source_required"
    return None


def _snapshot_member(item: RawItem, source: Source) -> dict[str, object]:
    document = _document(item).model_dump(mode="json")
    return {
        "raw_item_id": item.id,
        **document,
        "ownership": source.ownership,
        "high_risk": source.high_risk,
        "auto_publish": source.auto_publish,
    }


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    text: str
    message_id: int | None = None


class EditorialService:
    """Use a dedicated session: methods commit their own durable boundaries."""

    def __init__(self, session: Session, pipeline: EditorialPipeline) -> None:
        self.session = session
        self.pipeline = pipeline

    def prepare(self, raw_item_id: int) -> EditorialRun:
        try:
            city = self.session.scalar(select(Source.city).join(
                RawItem, RawItem.source_id == Source.id,
            ).where(RawItem.id == raw_item_id))
            if city is None:
                raise ValueError("raw item/source does not exist")
            _lock_editorial_city(self.session, city)
            item = self.session.scalar(
                select(RawItem).where(RawItem.id == raw_item_id).with_for_update()
                .execution_options(populate_existing=True)
            )
            if item is None:
                raise ValueError("raw item does not exist")
            existing = self.session.get(EditorialRun, raw_item_id)
            if existing is not None:
                self.session.commit()
                return existing
            source = self.session.get(Source, item.source_id, populate_existing=True)
            if source is None:
                raise ValueError("raw item source does not exist")
            if not source.enabled:
                raise ValueError("raw item source is disabled")
            reviewed_event = _reviewed_event_item(
                self.session,
                anchor=item,
                anchor_source=source,
            )
            if reviewed_event is not None:
                raise ValueError(
                    f"event already reviewed via raw item {reviewed_event.id}"
                )

            members = _event_items(self.session, anchor=item, anchor_source=source)
            documents = tuple(_document(member) for member, _member_source in members)
            outcome = self.pipeline.run(city=source.city, documents=documents)
            policy_reason = _auto_publish_policy_reason(
                members, anchor_source=source, category=outcome.ranking.category,
            )
            if outcome.status == PublicationStatus.READY and policy_reason is not None:
                outcome = outcome.model_copy(update={
                    "status": PublicationStatus.HUMAN_REVIEW,
                    "reason": policy_reason,
                })

            post = None
            if outcome.draft is not None and outcome.status in {
                PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW,
            }:
                post = Post(
                    city=source.city,
                    category=outcome.ranking.category,
                    status=outcome.status.value,
                    title=outcome.draft.title,
                    body=outcome.draft.body,
                    confidence=Decimal(str(outcome.confidence)),
                    editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
                )
                self.session.add(post)
                self.session.flush()

            anchor_document = documents[0]
            run = EditorialRun(
                raw_item_id=item.id,
                outcome=outcome.model_dump(mode="json"),
                evidence={
                    **anchor_document.model_dump(mode="json"),
                    "city": source.city,
                    "event_key": event_key(_candidate(item)),
                    "documents": [
                        _snapshot_member(member, member_source)
                        for member, member_source in members
                    ],
                },
                post_id=post.id if post else None,
                editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
                delivery_status="pending",
            )
            self.session.add(run)
            self.session.commit()
            return run
        except BaseException:
            self.session.rollback()
            raise

    def _validate_evidence_bundle(
        self,
        run: EditorialRun,
        *,
        anchor_item: RawItem,
        anchor_source: Source,
    ) -> list[tuple[RawItem, Source]]:
        snapshots = run.evidence.get("documents")
        if not isinstance(snapshots, list):
            return [(anchor_item, anchor_source)]

        current: list[tuple[RawItem, Source]] = []
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get("raw_item_id"), int):
                raise DeliveryBlockedError("invalid stored evidence bundle")
            member = self.session.get(RawItem, snapshot["raw_item_id"], populate_existing=True)
            if member is None:
                raise DeliveryBlockedError("event evidence disappeared after verification")
            source = self.session.get(Source, member.source_id, populate_existing=True)
            if source is None or not source.enabled:
                raise DeliveryBlockedError("event evidence source is missing or disabled")
            expected = {
                "source_id": member.source_id,
                "url": member.url,
                "title": member.title,
                "body": member.body,
            }
            if any(snapshot.get(key) != value for key, value in expected.items()):
                raise DeliveryBlockedError("event evidence changed after verification")
            saved = EvidenceDocument.model_validate(snapshot)
            if _timestamp(saved.published_at) != _timestamp(member.published_at):
                raise DeliveryBlockedError(
                    "event evidence publication time changed after verification"
                )
            current.append((member, source))
        return current

    async def deliver(
        self, raw_item_id: int, publisher: Publisher | None = None, *, dry_run: bool = True,
    ) -> DeliveryResult:
        city = self.session.scalar(select(Source.city).join(
            RawItem, RawItem.source_id == Source.id,
        ).where(RawItem.id == raw_item_id))
        if city is None:
            raise DeliveryBlockedError("raw item/source does not exist")
        _lock_editorial_city(self.session, city)
        run = self.session.get(EditorialRun, raw_item_id, populate_existing=True)
        if run is None:
            raise ValueError("prepare the raw item before delivery")
        outcome = EditorialOutcome.model_validate(run.outcome)
        if "_package" in run.outcome:
            from relocant_media.ai.package import EditorialPackage, evaluate_package

            package = EditorialPackage.model_validate_json(
                json.dumps(run.outcome["_package"])
            )
            outcome = evaluate_package(package)
        if outcome.status != PublicationStatus.READY or outcome.draft is None:
            raise DeliveryBlockedError(f"editorial policy blocks delivery: {outcome.status}")
        item = self.session.get(RawItem, raw_item_id, populate_existing=True)
        source = self.session.get(Source, item.source_id, populate_existing=True) if item else None
        post = self.session.get(Post, run.post_id, populate_existing=True) if run.post_id else None
        if item is None or source is None or not source.enabled:
            raise DeliveryBlockedError("source is missing or disabled")
        if source.city != run.evidence.get("city") or post is None or post.city != source.city:
            raise DeliveryBlockedError("editorial city does not match the source")
        if post.status != PublicationStatus.READY:
            raise DeliveryBlockedError("post is no longer ready for delivery")
        if (post.title, post.body) != (outcome.draft.title, outcome.draft.body):
            raise DeliveryBlockedError("post draft changed after verification")
        from relocant_media.domain.draft_language import draft_language_reason
        from relocant_media.domain.freshness import draft_date_reason

        if draft_language_reason(outcome.draft.title, outcome.draft.body) is not None:
            raise DeliveryBlockedError("draft requires Russian translation")
        if draft_date_reason(outcome.draft.title,outcome.draft.body) is not None:
            raise DeliveryBlockedError("draft requires an absolute event date")
        if any(run.evidence.get(key) != value for key, value in {
            "source_id": item.source_id, "url": item.url,
            "title": item.title, "body": item.body,
        }.items()):
            raise DeliveryBlockedError("source evidence changed after verification")
        saved_document = EvidenceDocument.model_validate(run.evidence)
        if _timestamp(saved_document.published_at) != _timestamp(item.published_at):
            raise DeliveryBlockedError("source publication time changed after verification")

        members = self._validate_evidence_bundle(run, anchor_item=item, anchor_source=source)
        if "_package" in run.outcome and package.version == 2:
            assert package.event is not None
            # A new source, changed membership or policy requires fresh bundle reviews.
            members = _event_items(self.session, anchor=item, anchor_source=source)
            expected = {d.raw_item_id: d.model_dump(mode="json")
                        for d in package.event.documents}
            actual = {member.id: _snapshot_member(member, policy)
                      for member, policy in members}
            if (package.event.event_key != event_key(_candidate(item))
                    or expected != actual):
                raise DeliveryBlockedError("event evidence or source policy changed")
        policy_reason = _auto_publish_policy_reason(
            members, anchor_source=source, category=outcome.ranking.category,
        )
        if policy_reason is not None:
            raise DeliveryBlockedError(f"source policy blocks delivery: {policy_reason}")

        primary = next(
            (member for member, member_source in members
             if member_source.ownership.strip().casefold() == "official"),
            item,
        )
        text = f"{outcome.draft.title}\n\n{outcome.draft.body}\n\nИсточник: {primary.url}"
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise DeliveryBlockedError("draft exceeds Telegram message length")
        if dry_run:
            return DeliveryResult("dry_run", text)
        if publisher is None:
            raise ValueError("a staging publisher is required")
        # Commit BEFORE the external request. A crash or timeout must never trigger an
        # automatic retry: Telegram sendMessage has no idempotency key.
        claimed = cast(CursorResult[object], self.session.execute(
            update(EditorialRun).where(
                EditorialRun.raw_item_id == raw_item_id,
                EditorialRun.delivery_status == "pending",
            ).values(delivery_status="sending")
        ))
        self.session.commit()
        if claimed.rowcount != 1:
            self.session.refresh(run)
            if run.delivery_status == "sent":
                return DeliveryResult("sent", text, run.staging_message_id)
            raise DeliveryBlockedError("delivery outcome requires manual reconciliation")
        try:
            message_id = await publisher.publish(text)
        except BaseException:
            self.session.execute(update(EditorialRun).where(
                EditorialRun.raw_item_id == raw_item_id,
            ).values(delivery_status="uncertain"))
            self.session.commit()
            raise
        self.session.execute(update(EditorialRun).where(
            EditorialRun.raw_item_id == raw_item_id,
        ).values(delivery_status="sent", staging_message_id=message_id))
        self.session.commit()
        return DeliveryResult("sent", text, message_id)
