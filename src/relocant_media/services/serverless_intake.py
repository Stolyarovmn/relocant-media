"""Import a captured Telegram Serverless inbox into the canonical editorial database."""

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.private_intake import process_private_updates
from relocant_media.services.reader_intake import DRAFT_STATES, ROUTES, _identity
from relocant_media.services.submission_reference import (
    PUBLIC_REFERENCE_PATTERN,
    submission_reference,
)
from relocant_media.storage.models import AdRequest, Submission, TelegramUpdateReceipt


def _import_draft(session: Session, entry: dict[str, Any], update_id: int) -> None:
    receipt = session.get(TelegramUpdateReceipt, update_id)
    if receipt is not None:
        if receipt.status not in {"processed", "ignored"}:
            raise ValueError("unresolved canonical Telegram receipt")
        return
    draft = entry["draft"]
    if not isinstance(draft, dict) or not isinstance(draft.get("payload"), dict):
        raise ValueError("invalid Serverless reader draft")
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
        or not isinstance(reference, str)
        or (not re.fullmatch(PUBLIC_REFERENCE_PATTERN, reference)
            and not (type(draft.get("id")) is int and reference == str(draft["id"])))
    ):
        raise ValueError("invalid Serverless reader draft")
    message = entry["update"].get("message", entry["update"].get("edited_message", {}))
    identity = _identity(message) if isinstance(message, dict) else None
    topic_id = payload.get("topic_id")
    if (identity is None or identity[0] != user_id or identity[1] != payload.get("chat_id")
            or type(payload.get("chat_id")) is not int
            or identity[3] != topic_id
            or type(draft.get("telegram_message_id")) is not int
            or draft["telegram_message_id"] <= 0
            or (topic_id is not None and (
                type(topic_id) is not int or type(payload.get("channel_id")) is not int
                or payload.get("channel_id") != APPROVED_CHANNEL_ID
                or payload.get("origin") != "channel_direct_messages"))
            or (topic_id is None and any(
                payload.get(key) is not None for key in ("channel_id", "origin")))):
        raise ValueError("Serverless draft identity does not match the Telegram update")
    old_id = draft.get("id")
    if old_id is not None and (type(old_id) is not int or old_id <= 0):
        raise ValueError("invalid legacy reader draft id")
    row = session.get(Submission, old_id) if type(old_id) is int else session.scalar(
        select(Submission).where(Submission.payload["public_reference"].as_string() == reference)
    )
    if old_id is not None and row is None:
        raise ValueError("the legacy reader draft is missing from the editorial snapshot")
    if row is not None and (
        row.telegram_user_id != user_id or submission_reference(row) != reference
        or row.payload.get("chat_id") != payload.get("chat_id")
        or row.payload.get("topic_id") != payload.get("topic_id")
        or row.payload.get("channel_id") != payload.get("channel_id")
        or row.status not in DRAFT_STATES
    ):
        raise ValueError("the existing reader draft changed in the editorial queue")
    notices = payload.get("reader_notices", {})
    if not isinstance(notices, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        or value.get("status") not in {"sent", "uncertain", "superseded"}
        for key, value in notices.items()
    ):
        raise ValueError("Serverless notification delivery is still in progress")
    notices = dict(notices)
    if notice := entry.get("notice"):
        if (not isinstance(notice, dict) or notice.get("status") not in {"sent", "uncertain"}
                or not isinstance(notice.get("key"), str) or not notice["key"]):
            raise ValueError("Serverless notification delivery is still in progress")
        notices[notice["key"]] = notice
    if row is not None:
        for key, previous in row.payload.get("reader_notices", {}).items():
            if previous.get("status") in {"sent", "uncertain"}:
                if key in notices and notices[key] != previous:
                    raise ValueError("cannot replace a terminal reader delivery receipt")
                notices[key] = previous
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
    after: int | None = None,
) -> int | None:
    """Replay ordered envelopes. The caller acknowledges the cloud only after checkpointing."""
    with Session(engine) as session:
        if session.scalar(select(TelegramUpdateReceipt.update_id).where(
            TelegramUpdateReceipt.status.in_(("processing", "failed")),
        ).limit(1)) is not None:
            raise ValueError("unresolved canonical Telegram receipts")
    previous = after
    # Check the complete sequence before the first mutation or owner acknowledgement.
    for index, entry in enumerate(events):
        if not isinstance(entry, dict):
            raise ValueError("invalid Serverless envelope")
        sequence = entry.get("sequence")
        if (type(sequence) is not int or sequence < 1
                or (index > 0 and sequence != events[index - 1]["sequence"] + 1)
                or (index == 0 and after is not None and sequence != after + 1)):
            raise ValueError("invalid sequence in the Serverless inbox")
    for entry in events:
        sequence = entry.get("sequence")
        update = entry.get("update", {})
        update_id = update.get("update_id") if isinstance(update, dict) else None
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
