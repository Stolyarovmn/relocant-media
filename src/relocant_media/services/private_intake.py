"""Receive private tips and exact owner-review actions without publication side effects."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.intake import handle_private_message
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.owner_review import (
    OwnerUpdateResult,
    record_owner_callback,
    record_owner_reply,
)
from relocant_media.services.submission_feedback import save_requested_followup
from relocant_media.storage.models import Submission, TelegramUpdateReceipt


def save_channel_tip(
    session: Session,
    message: dict[str, Any],
    *,
    edited: bool = False,
) -> bool:
    sender = message.get("from", {})
    topic = message.get("direct_messages_topic", {})
    chat = message.get("chat", {})
    user_id = sender.get("id")
    message_id = message.get("message_id")
    if (
        type(user_id) is not int
        or user_id <= 0
        or sender.get("is_bot")
        or type(message_id) is not int
        or message_id <= 0
        or type(topic.get("topic_id")) is not int
        or type(chat.get("id")) is not int
    ):
        return False
    body = message.get("text", message.get("caption", ""))
    if not isinstance(body, str) or len(body) > 20_000:
        return False
    kind = "correction" if edited else "news_tip"
    if body.startswith("/ad "):
        kind = "ad_request"
    payload = {
        "text": body,
        "chat_id": chat["id"],
        "topic_id": topic["topic_id"],
        "origin": "channel_direct_messages",
        "city": "belgrade",
        "channel_id": APPROVED_CHANNEL_ID,
        "edited": edited,
        "media_present": any(
            key in message for key in ("photo", "video", "document", "voice")
        ),
    }
    attachments: list[dict[str, object]] = []
    for key in ("photo", "video", "document", "voice"):
        value = message.get(key)
        if key == "photo" and isinstance(value, list) and value:
            value = value[-1]
        if isinstance(value, dict) and isinstance(value.get("file_id"), str):
            attachments.append(
                {
                    "kind": key,
                    "file_id": value["file_id"],
                    "file_unique_id": value.get("file_unique_id"),
                }
            )
    payload["attachments"] = attachments
    session.add(
        Submission(
            kind=kind,
            telegram_user_id=user_id,
            telegram_message_id=message_id,
            payload=payload,
            status="new",
        )
    )
    return True


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("invalid Telegram object")
    return value


async def _ack_owner_action(
    bot: TelegramBotAPI,
    callback: object,
    result: OwnerUpdateResult,
) -> None:
    """Best-effort UI acknowledgement after the durable decision has committed."""
    if isinstance(callback, dict):
        callback_id = callback.get("id")
        if isinstance(callback_id, str) and callback_id:
            try:
                await bot._call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_id,
                        "text": result.answer_text or "Принято.",
                    },
                )
            except Exception:
                pass
    if result.clear_chat_id is not None and result.clear_message_id is not None:
        try:
            await bot._call(
                "editMessageReplyMarkup",
                {
                    "chat_id": result.clear_chat_id,
                    "message_id": result.clear_message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except Exception:
            pass


async def receive_private_tips(
    engine: Engine,
    bot: TelegramBotAPI,
    *,
    enabled: bool,
    poll_timeout: int = 0,
    store_guard: Callable[[], AbstractContextManager[bool]] | None = None,
    accept_private_tips: bool = True,
    owner_user_id: int | None = None,
    owner_chat_id: int | None = None,
) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled", "received": 0, "owner_action_required": False}
    webhook = _mapping(await bot._call("getWebhookInfo", {}))
    if webhook.get("url"):
        return {
            "status": "blocked_existing_webhook",
            "received": 0,
            "owner_action_required": True,
        }
    if accept_private_tips:
        me = _mapping(await bot._call("getMe", {}))
        member = _mapping(
            await bot._call(
                "getChatMember",
                {"chat_id": APPROVED_CHANNEL_ID, "user_id": me["id"]},
            )
        )
        if member.get("can_manage_direct_messages") is not True:
            return {
                "status": "blocked_direct_message_access",
                "received": 0,
                "owner_action_required": True,
            }
    with Session(engine) as session:
        unresolved = session.scalar(
            select(TelegramUpdateReceipt.update_id)
            .where(TelegramUpdateReceipt.status.in_(("processing", "failed")))
            .limit(1)
        )
        if unresolved is not None:
            return {
                "status": "blocked_unresolved_receipts",
                "received": 0,
                "owner_action_required": False,
            }
        previous = session.scalar(select(func.max(TelegramUpdateReceipt.update_id)))
    if type(poll_timeout) is not int or not 0 <= poll_timeout <= 30:
        raise ValueError("poll timeout must be between 0 and 30")
    params: dict[str, object] = {"limit": 100, "timeout": poll_timeout}
    if previous is not None:
        params["offset"] = previous + 1
    # Never drop pending updates or change the server-side allowed_updates filter.
    updates = await bot._call("getUpdates", params)
    if not isinstance(updates, list) or len(updates) > 100:
        raise ValueError("invalid Telegram update batch")
    if not updates:
        return {
            "status": "ready",
            "received": 0,
            "inspected": 0,
            "owner_action_required": False,
            "telegram_sends": 0,
        }
    guard = store_guard() if store_guard else nullcontext(True)
    # Long polling never holds worker.lock. A busy worker leaves updates unacknowledged.
    with guard as acquired:
        if not acquired:
            return {
                "status": "busy",
                "received": 0,
                "inspected": 0,
                "owner_action_required": False,
                "telegram_sends": 0,
            }
        return await _store_updates(
            engine,
            bot,
            updates,
            accept_private_tips=accept_private_tips,
            owner_user_id=owner_user_id,
            owner_chat_id=owner_chat_id,
        )


async def _store_updates(
    engine: Engine,
    bot: TelegramBotAPI,
    updates: list[dict[str, Any]],
    *,
    accept_private_tips: bool = True,
    owner_user_id: int | None = None,
    owner_chat_id: int | None = None,
) -> dict[str, Any]:
    received = 0
    owner_actions = 0
    parents: dict[int, bool] = {}
    for update in sorted(updates, key=lambda value: value["update_id"]):
        uid = update.get("update_id")
        if type(uid) is not int:
            raise ValueError("invalid Telegram update id")
        owner_result: OwnerUpdateResult | None = None
        callback = update.get("callback_query")
        with Session(engine) as session:
            if session.get(TelegramUpdateReceipt, uid) is not None:
                continue
            if callback is not None:
                owner_result = record_owner_callback(
                    session,
                    callback,
                    owner_user_id=owner_user_id,
                    owner_chat_id=owner_chat_id,
                )
                if owner_result is not None and owner_result.handled:
                    session.add(
                        TelegramUpdateReceipt(
                            update_id=uid,
                            kind="owner_review",
                            status="processed" if owner_result.accepted else "ignored",
                            processed_at=datetime.now(UTC),
                        )
                    )
                    session.commit()
                    received += int(owner_result.accepted)
                    owner_actions += int(owner_result.accepted)
            if owner_result is None:
                message = update.get("message", update.get("edited_message", {}))
                chat = message.get("chat", {}) if isinstance(message, dict) else {}
                accepted = False
                receipt_kind = "private_intake"
                if isinstance(message, dict) and "message" in update:
                    owner_result = record_owner_reply(
                        session,
                        message,
                        owner_user_id=owner_user_id,
                        owner_chat_id=owner_chat_id,
                    )
                    if owner_result is not None and owner_result.handled:
                        accepted = owner_result.accepted
                        receipt_kind = "owner_review"
                if owner_result is None:
                    if (
                        accept_private_tips
                        and chat.get("is_direct_messages") is True
                        and type(chat.get("id")) is int
                    ):
                        cid = chat["id"]
                        if cid not in parents:
                            info = _mapping(
                                await bot._call("getChat", {"chat_id": cid})
                            )
                            parent = info.get("parent_chat", {})
                            parents[cid] = (
                                isinstance(parent, dict)
                                and parent.get("id") == APPROVED_CHANNEL_ID
                            )
                        if parents[cid]:
                            accepted = save_channel_tip(
                                session,
                                message,
                                edited="edited_message" in update,
                            )
                    elif (
                        accept_private_tips
                        and chat.get("type") == "private"
                        and "message" in update
                    ):
                        accepted = save_requested_followup(
                            session, message
                        ) or handle_private_message(
                            session, message, city="belgrade"
                        ).outcome == "submitted"
                session.add(
                    TelegramUpdateReceipt(
                        update_id=uid,
                        kind=receipt_kind,
                        status="processed" if accepted else "ignored",
                        processed_at=datetime.now(UTC),
                    )
                )
                # Receipt and owner decision/submission commit together, before next offset.
                session.commit()
                received += int(accepted)
                if receipt_kind == "owner_review":
                    owner_actions += int(accepted)
        if owner_result is not None and owner_result.handled:
            await _ack_owner_action(bot, callback, owner_result)
    return {
        "status": "ready",
        "received": received,
        "inspected": len(updates),
        "owner_actions": owner_actions,
        "owner_action_required": False,
        "telegram_sends": 0,
    }
