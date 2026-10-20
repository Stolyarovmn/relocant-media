"""Explicitly enabled, at-most-once delivery of reviewed channel packages."""

import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.publisher import Publisher
from relocant_media.services.editorial import DeliveryBlockedError, DeliveryResult, EditorialService
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

APPROVED_CHANNEL_ID = -1003967014133


async def deliver_reviewed(
    session: Session, raw_item_id: int, publisher: Publisher, *,
    target_channel_id: int, enabled: bool = False,
) -> DeliveryResult:
    """Dedicated session and caller-injected target adapter; no staging bypass."""
    if enabled is not True:
        raise DeliveryBlockedError("channel delivery is disabled")
    if type(target_channel_id) is not int or target_channel_id != APPROVED_CHANNEL_ID:
        raise DeliveryBlockedError("channel target is not approved")
    try:
        city = session.scalar(select(Source.city).join(
            RawItem, RawItem.source_id == Source.id,
        ).where(RawItem.id == raw_item_id))
        if city is None:
            raise DeliveryBlockedError("raw item/source does not exist")
        lock_editorial_city(session, city)
        run = session.get(EditorialRun, raw_item_id, populate_existing=True)
        post = (session.get(Post, run.post_id, populate_existing=True)
                if run and run.post_id else None)
        if run is None or post is None:
            raise DeliveryBlockedError("reviewed post is missing")
        if run.delivery_status == "sent":
            if (type(post.telegram_message_id) is not int or post.telegram_message_id <= 0
                    or run.staging_message_id is not None):
                raise DeliveryBlockedError("channel receipt requires manual reconciliation")
            result = DeliveryResult("sent", "", post.telegram_message_id)
            session.commit()
            return result
        if (run.delivery_status != "pending" or post.telegram_message_id is not None
                or run.staging_message_id is not None):
            raise DeliveryBlockedError("delivery outcome requires manual reconciliation")
        if "_package" not in run.outcome:
            raise DeliveryBlockedError("reviewed event package is required")
        package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
        if package.raw_item_id != raw_item_id or package.city != city:
            raise DeliveryBlockedError("reviewed package identity does not match")
        if package.version != 2 or any(review.verdict != "approve" for review in package.reviews):
            raise DeliveryBlockedError("two event bundle approvals are required")
        if not post.title.startswith("[тестовый запуск]"):
            raise DeliveryBlockedError("test launch title prefix is required")
        service = EditorialService(session, cast(EditorialPipeline, None))
        preview = await service.deliver(raw_item_id, dry_run=True)
        claimed = cast(CursorResult[object], session.execute(update(EditorialRun).where(
            EditorialRun.raw_item_id == raw_item_id,
            EditorialRun.delivery_status == "pending",
        ).values(delivery_status="sending")))
        if claimed.rowcount != 1:
            raise DeliveryBlockedError("delivery claim failed")
        session.commit()
    except BaseException:
        session.rollback()
        raise
    try:
        message_id = await publisher.publish(preview.text)
        if type(message_id) is not int or message_id <= 0:
            raise DeliveryBlockedError("publisher returned an invalid receipt")
    except BaseException:
        session.execute(update(EditorialRun).where(
            EditorialRun.raw_item_id == raw_item_id,
        ).values(delivery_status="uncertain"))
        session.commit()
        raise
    post.telegram_message_id = message_id
    post.published_at = datetime.now(UTC)
    run.delivery_status = "sent"
    run.staging_message_id = None
    session.commit()
    return DeliveryResult("sent", preview.text, message_id)
