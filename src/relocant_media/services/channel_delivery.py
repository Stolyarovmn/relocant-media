"""Explicitly enabled, at-most-once delivery of reviewed channel packages."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.media_publisher import MediaBot, publish_media
from relocant_media.bot.publisher import Publisher
from relocant_media.domain.presentation import valid_title_presentation
from relocant_media.services.editorial import DeliveryBlockedError, DeliveryResult, EditorialService
from relocant_media.services.production_media import (
    PreparedProductionMedia,
    prepare_production_media,
)
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

APPROVED_CHANNEL_ID = -1003967014133


def _media_receipt(
    prepared: PreparedProductionMedia,
    *,
    state: str,
    message_ids: list[int] | None = None,
) -> dict[str, object]:
    return {
        "state": state,
        "release_id": prepared.release.release_id,
        "release_sha256": prepared.release_sha256,
        "asset_sha256": [asset.sha256 for asset in prepared.release.assets],
        "planned_message_count": len(prepared.uploads),
        "message_ids": message_ids or [],
    }


async def deliver_reviewed(
    session: Session, raw_item_id: int, publisher: Publisher, *,
    target_channel_id: int, enabled: bool = False,
) -> DeliveryResult:
    """Dedicated session and caller-injected target adapter; no staging bypass."""
    if enabled is not True:
        raise DeliveryBlockedError("channel delivery is disabled")
    if type(target_channel_id) is not int or target_channel_id != APPROVED_CHANNEL_ID:
        raise DeliveryBlockedError("channel target is not approved")
    prepared_media: PreparedProductionMedia | None = None
    media_bot: MediaBot | None = None
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
        if not valid_title_presentation(post.title):
            raise DeliveryBlockedError("title presentation does not match channel mode")
        package_sha256 = hashlib.sha256(
            canonical_json(package.model_dump(mode="json")).encode()
        ).hexdigest()
        if run.outcome.get("_package_sha256") != package_sha256:
            raise DeliveryBlockedError("reviewed package fingerprint does not match")
        service = EditorialService(session, cast(EditorialPipeline, None))
        preview = await service.deliver(raw_item_id, dry_run=True)
        try:
            prepared_media = prepare_production_media(
                raw_item_id=raw_item_id,
                post_id=post.id,
                editorial_package_sha256=package_sha256,
                target_channel_id=target_channel_id,
                editorial_text=preview.text,
                now=datetime.now(UTC),
            )
        except ValueError as error:
            raise DeliveryBlockedError("reviewed production media bundle is invalid") from error
        if prepared_media is not None:
            candidate_bot = getattr(publisher, "bot", None)
            if (
                candidate_bot is None
                or not callable(getattr(candidate_bot, "send_photo", None))
                or not callable(getattr(candidate_bot, "send_media_group", None))
            ):
                raise DeliveryBlockedError("production publisher does not support reviewed media")
            media_bot = cast(MediaBot, candidate_bot)
            run.outcome = {
                **run.outcome,
                "_media_delivery": _media_receipt(prepared_media, state="sending"),
            }
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
    message_ids: list[int] | None = None
    try:
        if prepared_media is None:
            message_id = await publisher.publish(preview.text)
            if type(message_id) is not int or message_id <= 0:
                raise DeliveryBlockedError("publisher returned an invalid receipt")
        else:
            assert media_bot is not None
            message_ids = await publish_media(
                media_bot,
                target_channel_id,
                prepared_media.caption,
                prepared_media.uploads,
            )
            if (
                len(message_ids) != len(prepared_media.uploads)
                or len(set(message_ids)) != len(message_ids)
                or any(type(value) is not int or value <= 0 for value in message_ids)
            ):
                raise DeliveryBlockedError("media publisher returned an invalid receipt")
            message_id = message_ids[0]
    except BaseException:
        current = session.get(EditorialRun, raw_item_id, populate_existing=True)
        if current is not None:
            current.delivery_status = "uncertain"
            if prepared_media is not None:
                current.outcome = {
                    **current.outcome,
                    "_media_delivery": _media_receipt(prepared_media, state="uncertain"),
                }
        session.commit()
        raise
    post.telegram_message_id = message_id
    post.published_at = datetime.now(UTC)
    run.delivery_status = "sent"
    run.staging_message_id = None
    if prepared_media is not None:
        assert message_ids is not None
        run.outcome = {
            **run.outcome,
            "_media_delivery": _media_receipt(
                prepared_media,
                state="sent",
                message_ids=message_ids,
            ),
        }
    session.commit()
    delivered_text = prepared_media.caption if prepared_media is not None else preview.text
    return DeliveryResult("sent", delivered_text, message_id)
