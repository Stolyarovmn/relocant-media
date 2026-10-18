"""Persisted editorial processing and conservative, at-most-once staging delivery."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.publisher import Publisher
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source


class DeliveryBlockedError(ValueError):
    pass


def _timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


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
            item = self.session.scalar(
                select(RawItem).where(RawItem.id == raw_item_id).with_for_update()
            )
            if item is None:
                raise ValueError("raw item does not exist")
            existing = self.session.get(EditorialRun, raw_item_id)
            if existing is not None:
                self.session.commit()
                return existing
            source = self.session.get(Source, item.source_id)
            if source is None:
                raise ValueError("raw item source does not exist")
            if not source.enabled:
                raise ValueError("raw item source is disabled")
            document = EvidenceDocument(
                source_id=source.id, url=item.url, title=item.title,
                body=item.body, published_at=item.published_at,
            )
            outcome = self.pipeline.run(city=source.city, documents=(document,))
            post = None
            if outcome.draft is not None and outcome.status in {
                PublicationStatus.READY, PublicationStatus.HUMAN_REVIEW,
            }:
                post = Post(
                    city=source.city, category=outcome.ranking.category,
                    status=outcome.status.value, title=outcome.draft.title,
                    body=outcome.draft.body, confidence=Decimal(str(outcome.confidence)),
                )
                self.session.add(post)
                self.session.flush()
            run = EditorialRun(
                raw_item_id=item.id, outcome=outcome.model_dump(mode="json"),
                evidence={**document.model_dump(mode="json"), "city": source.city},
                post_id=post.id if post else None, delivery_status="pending",
            )
            self.session.add(run)
            self.session.commit()
            return run
        except BaseException:
            self.session.rollback()
            raise

    async def deliver(
        self, raw_item_id: int, publisher: Publisher | None = None, *, dry_run: bool = True,
    ) -> DeliveryResult:
        run = self.session.get(EditorialRun, raw_item_id)
        if run is None:
            raise ValueError("prepare the raw item before delivery")
        outcome = EditorialOutcome.model_validate(run.outcome)
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
        if any(run.evidence.get(key) != value for key, value in {
            "source_id": item.source_id, "url": item.url,
            "title": item.title, "body": item.body,
        }.items()):
            raise DeliveryBlockedError("source evidence changed after verification")
        saved_document = EvidenceDocument.model_validate(run.evidence)
        if _timestamp(saved_document.published_at) != _timestamp(item.published_at):
            raise DeliveryBlockedError("source publication time changed after verification")
        text = f"{outcome.draft.title}\n\n{outcome.draft.body}\n\nИсточник: {run.evidence['url']}"
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
