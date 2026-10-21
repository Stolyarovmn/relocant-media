"""Receive private tips without replies, forwarding or publication capabilities."""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.intake import handle_private_message
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.storage.models import Submission, TelegramUpdateReceipt


def save_channel_tip(session: Session, message: dict[str, Any], *, edited: bool = False) -> bool:
    sender, topic = message.get("from", {}), message.get("direct_messages_topic", {})
    chat = message.get("chat", {})
    user_id, message_id = sender.get("id"), message.get("message_id")
    if (type(user_id) is not int or user_id <= 0 or sender.get("is_bot")
            or type(message_id) is not int or message_id <= 0
            or type(topic.get("topic_id")) is not int or type(chat.get("id")) is not int):
        return False
    body = message.get("text", message.get("caption", ""))
    if not isinstance(body, str) or len(body) > 20_000:
        return False
    kind = "correction" if edited else "news_tip"
    if body.startswith("/ad "):
        kind = "ad_request"
    payload = dict(text=body, chat_id=chat["id"], topic_id=topic["topic_id"],
                   origin="channel_direct_messages", city="belgrade",
                   channel_id=APPROVED_CHANNEL_ID, edited=edited,
                   media_present=any(k in message for k in ("photo", "video", "document", "voice")))
    attachments = []
    for key in ("photo", "video", "document", "voice"):
        value = message.get(key)
        if key == "photo" and isinstance(value, list) and value:
            value = value[-1]
        if isinstance(value, dict) and isinstance(value.get("file_id"), str):
            attachments.append({"kind": key, "file_id": value["file_id"],
                                "file_unique_id": value.get("file_unique_id")})
    payload["attachments"] = attachments
    session.add(Submission(kind=kind, telegram_user_id=user_id,
        telegram_message_id=message_id, payload=payload, status="new"))
    return True


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid Telegram object")
    return value


async def receive_private_tips(
    engine: Engine, bot: TelegramBotAPI, *, enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return dict(status="disabled", received=0, owner_action_required=False)
    webhook = _mapping(await bot._call("getWebhookInfo", {}))
    if webhook.get("url"):
        return dict(status="blocked_existing_webhook", received=0, owner_action_required=True)
    me = _mapping(await bot._call("getMe", {}))
    member = _mapping(await bot._call(
        "getChatMember", dict(chat_id=APPROVED_CHANNEL_ID, user_id=me["id"])))
    if member.get("can_manage_direct_messages") is not True:
        return dict(status="blocked_direct_message_access", received=0, owner_action_required=True)
    with Session(engine) as session:
        unresolved = session.scalar(select(TelegramUpdateReceipt.update_id).where(
            TelegramUpdateReceipt.status.in_(("processing", "failed"))).limit(1))
        if unresolved is not None:
            return dict(status="blocked_unresolved_receipts", received=0,
                        owner_action_required=False)
        previous = session.scalar(select(func.max(TelegramUpdateReceipt.update_id)))
    params: dict[str, Any] = dict(limit=100, timeout=0)
    if previous is not None:
        params["offset"] = previous + 1
    # Never drop pending updates or change the server-side allowed_updates filter.
    updates = await bot._call("getUpdates", params)
    if not isinstance(updates, list) or len(updates) > 100:
        raise ValueError("invalid Telegram update batch")
    received = 0
    parents: dict[int, bool] = {}
    for update in sorted(updates, key=lambda value: value["update_id"]):
        uid = update.get("update_id")
        if type(uid) is not int:
            raise ValueError("invalid Telegram update id")
        with Session(engine) as session:
            if session.get(TelegramUpdateReceipt, uid) is not None:
                continue
            message = update.get("message", update.get("edited_message", {}))
            chat = message.get("chat", {})
            accepted = False
            if chat.get("is_direct_messages") is True and type(chat.get("id")) is int:
                cid = chat["id"]
                if cid not in parents:
                    info = _mapping(await bot._call("getChat", dict(chat_id=cid)))
                    parents[cid] = info.get("parent_chat", {}).get("id") == APPROVED_CHANNEL_ID
                if parents[cid]:
                    accepted = save_channel_tip(session, message, edited="edited_message" in update)
            elif chat.get("type") == "private" and "message" in update:
                accepted = handle_private_message(
                    session, message, city="belgrade").outcome == "submitted"
            session.add(TelegramUpdateReceipt(update_id=uid, kind="private_intake",
                status="processed" if accepted else "ignored", processed_at=datetime.now(UTC)))
            # Receipt and submission commit together, before the next offset can acknowledge them.
            session.commit()
            received += int(accepted)
    return dict(status="ready", received=received, inspected=len(updates),
                owner_action_required=False, telegram_sends=0)
