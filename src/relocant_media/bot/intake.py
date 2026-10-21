from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from relocant_media.domain.models import SubmissionKind
from relocant_media.storage.models import AdRequest, Submission, TelegramConversation

COMMAND_KINDS = {
    "/tip": SubmissionKind.NEWS_TIP,
    "/correction": SubmissionKind.CORRECTION,
    "/ad": SubmissionKind.AD_REQUEST,
}


@dataclass(frozen=True, slots=True)
class IntakeResult:
    outcome: str
    reply: str | None = None


def _int_value(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _message_identity(message: dict[str, Any]) -> tuple[int, int] | None:
    sender = message.get("from")
    if not isinstance(sender, dict):
        return None
    user_id = _int_value(sender.get("id"))
    message_id = _int_value(message.get("message_id"))
    if user_id is None or message_id is None:
        return None
    return user_id, message_id


def _save_submission(
    session: Session,
    kind: SubmissionKind,
    user_id: int,
    message_id: int,
    chat_id: int | None,
    text: str,
    city: str | None = None,
) -> None:
    payload: dict[str, Any] = {"text": text}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    if city:
        payload["city"] = city
    session.add(
        Submission(
            kind=kind.value,
            telegram_user_id=user_id,
            telegram_message_id=message_id,
            payload=payload,
            status="new",
        )
    )
    if kind is SubmissionKind.AD_REQUEST:
        session.add(AdRequest(telegram_user_id=user_id, payload=payload, status="new"))


def handle_private_message(
    session: Session, message: dict[str, Any], *, city: str | None = None,
) -> IntakeResult:
    identity = _message_identity(message)
    text_value = message.get("text")
    if identity is None or not isinstance(text_value, str):
        return IntakeResult(outcome="ignored")

    user_id, message_id = identity
    text = text_value.strip()
    if not text:
        return IntakeResult(outcome="ignored")

    chat = message.get("chat")
    chat_id = _int_value(chat.get("id")) if isinstance(chat, dict) else None
    command, separator, remainder = text.partition(" ")
    normalized_command = command.split("@", maxsplit=1)[0].lower()

    if normalized_command == "/cancel":
        conversation = session.get(TelegramConversation, user_id)
        if conversation is not None:
            session.delete(conversation)
        return IntakeResult(outcome="cancelled", reply="Сбор сообщения отменён.")

    selected_kind = COMMAND_KINDS.get(normalized_command)
    if selected_kind is not None:
        body = remainder.strip() if separator else ""
        if body:
            _save_submission(session, selected_kind, user_id, message_id, chat_id, body, city)
            conversation = session.get(TelegramConversation, user_id)
            if conversation is not None:
                session.delete(conversation)
            return IntakeResult(outcome="submitted", reply="Спасибо, сообщение принято.")

        conversation = session.get(TelegramConversation, user_id)
        if conversation is None:
            conversation = TelegramConversation(
                telegram_user_id=user_id,
                pending_kind=selected_kind.value,
            )
            session.add(conversation)
        else:
            conversation.pending_kind = selected_kind.value
        return IntakeResult(
            outcome="awaiting_text",
            reply="Пришлите следующим сообщением текст. /cancel — отмена.",
        )

    conversation = session.get(TelegramConversation, user_id)
    if conversation is None:
        return IntakeResult(outcome="ignored")

    pending_kind = SubmissionKind(conversation.pending_kind)
    _save_submission(session, pending_kind, user_id, message_id, chat_id, text, city)
    session.delete(conversation)
    return IntakeResult(outcome="submitted", reply="Спасибо, сообщение принято.")
