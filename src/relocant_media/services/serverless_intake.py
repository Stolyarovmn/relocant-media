"""Import a captured Telegram Serverless inbox into the canonical editorial database."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.private_intake import process_private_updates
from relocant_media.services.reader_intake import DRAFT_STATES, ROUTES
from relocant_media.services.submission_reference import submission_reference
from relocant_media.storage.models import AdRequest, Submission, TelegramUpdateReceipt


def _import_draft(session: Session, entry: dict[str, Any], update_id: int) -> None:
    if session.get(TelegramUpdateReceipt, update_id) is not None:
        return
    draft = entry["draft"]
    payload = dict(draft["payload"])
    reference = draft["reference"]
    user_id = draft["telegram_user_id"]
    status = draft["status"]
    kind = draft["kind"]
    if (
        type(user_id) is not int or user_id <= 0
        or status not in (*DRAFT_STATES, "new", "cancelled")
        or kind not in (*ROUTES, "reader_unclassified")
        or payload.get("reader_intake") is not True
        or payload.get("public_reference") != reference
    ):
        raise ValueError("invalid Serverless reader draft")
    message = entry["update"].get("message", entry["update"].get("edited_message", {}))
    if (
        message.get("from", {}).get("id") != user_id
        or message.get("chat", {}).get("id") != payload.get("chat_id")
        or message.get("direct_messages_topic", {}).get("topic_id") != payload.get("topic_id")
        or (payload.get("topic_id") and payload.get("channel_id") != APPROVED_CHANNEL_ID)
    ):
        raise ValueError("Serverless draft identity does not match the Telegram update")
    old_id = draft.get("id")
    row = session.get(Submission, old_id) if type(old_id) is int else session.scalar(
        select(Submission).where(Submission.payload["public_reference"].as_string() == reference)
    )
    if old_id is not None and row is None:
        raise ValueError("the legacy reader draft is missing from the editorial snapshot")
    if row is not None and (
        row.telegram_user_id != user_id or submission_reference(row) != reference
        or row.payload.get("chat_id") != payload.get("chat_id")
        or row.payload.get("topic_id") != payload.get("topic_id")
        or row.status not in DRAFT_STATES
    ):
        raise ValueError("the existing reader draft changed in the editorial queue")
    if notice := entry.get("notice"):
        if notice["status"] not in {"sent", "uncertain"}:
            raise ValueError("Serverless notification delivery is still in progress")
        notices = dict(payload.get("reader_notices", {}))
        notices[notice["key"]] = notice
        payload["reader_notices"] = notices
    # The cloud owns intake notices on this path, including uncertain sends.
    payload["reader_notice_pending"] = False
    if row is None:
        row = Submission(telegram_user_id=user_id, payload=payload, status=status, kind=kind)
        session.add(row)
    row.telegram_message_id = draft["telegram_message_id"]
    row.payload = payload
    row.status = status
    row.kind = kind
    session.flush()
    if status == "new" and kind == "ad_request":
        session.add(AdRequest(
            telegram_user_id=user_id, payload={**payload, "submission_id": row.id}, status="new",
        ))
    session.add(TelegramUpdateReceipt(
        update_id=update_id, kind="reader_intake", status="processed",
        processed_at=datetime.now(UTC),
    ))
    session.commit()


async def import_inbox(
    engine: Engine,
    bot: TelegramBotAPI,
    events: list[dict[str, Any]],
    *,
    owner_user_id: int | None,
    owner_chat_id: int | None,
) -> int | None:
    """Replay ordered envelopes. The caller acknowledges the cloud only after checkpointing."""
    previous: int | None = None
    for entry in events:
        sequence = entry.get("sequence")
        update = entry.get("update", {})
        update_id = update.get("update_id")
        if (
            type(sequence) is not int or sequence < 1
            or (previous is not None and sequence <= previous)
            or type(update_id) is not int or update_id < 0
        ):
            raise ValueError("invalid sequence in the Serverless inbox")
        previous = sequence
        if entry.get("draft") is None:
            await process_private_updates(
                engine, bot, [update], reader_feedback_enabled=True,
                owner_user_id=owner_user_id, owner_chat_id=owner_chat_id,
            )
        else:
            with Session(engine) as session:
                _import_draft(session, entry, update_id)
    return previous
