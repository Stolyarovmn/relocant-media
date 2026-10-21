from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBot
from relocant_media.bot.intake import handle_private_message
from relocant_media.bot.updates import UpdateKind, classify_update, update_id
from relocant_media.services.moderation import ModerationAction as RuleAction
from relocant_media.services.moderation import evaluate_moderation
from relocant_media.storage.models import ModerationAction, TelegramUpdateReceipt


@dataclass(frozen=True, slots=True)
class TelegramProcessingResult:
    update_id: int
    kind: UpdateKind
    status: str
    action: str


def _dict(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return None


def _int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _claim_update(session: Session, telegram_update_id: int, kind: UpdateKind) -> bool:
    statement = (
        insert(TelegramUpdateReceipt)
        .values(update_id=telegram_update_id, kind=kind.value, status="processing")
        .on_conflict_do_nothing(index_elements=["update_id"])
        .returning(TelegramUpdateReceipt.update_id)
    )
    claimed = session.execute(statement).scalar_one_or_none()
    session.commit()
    return claimed is not None


def _finish_update(
    session: Session,
    telegram_update_id: int,
    status: str,
    error: str | None,
) -> None:
    receipt = session.get(TelegramUpdateReceipt, telegram_update_id)
    if receipt is None:
        raise RuntimeError("Telegram update receipt disappeared during processing")
    receipt.status = status
    receipt.error = error
    receipt.processed_at = datetime.now(UTC)
    session.commit()


class TelegramRuntime:
    def __init__(
        self,
        session: Session,
        bot: TelegramBot,
        city: str = "",
        staging_discussion_group_id: str = "",
    ) -> None:
        self._session = session
        self._bot = bot
        self._city = city
        self._discussion_group_id = staging_discussion_group_id

    async def process(self, payload: dict[str, Any]) -> TelegramProcessingResult:
        telegram_update_id = update_id(payload)
        if telegram_update_id is None:
            raise ValueError("Telegram update_id is required")
        kind = classify_update(payload)

        if not _claim_update(self._session, telegram_update_id, kind):
            return TelegramProcessingResult(
                update_id=telegram_update_id,
                kind=kind,
                status="duplicate",
                action="none",
            )

        try:
            action = await self._dispatch(kind, payload)
            _finish_update(self._session, telegram_update_id, "processed", None)
        except Exception as error:
            self._session.rollback()
            _finish_update(self._session, telegram_update_id, "failed", str(error))
            raise

        return TelegramProcessingResult(
            update_id=telegram_update_id,
            kind=kind,
            status="processed",
            action=action,
        )

    async def _dispatch(self, kind: UpdateKind, payload: dict[str, Any]) -> str:
        if kind is not UpdateKind.MESSAGE:
            return "ignored"
        message = _dict(payload.get("message"))
        if message is None:
            return "ignored"
        chat = _dict(message.get("chat"))
        if chat is None:
            return "ignored"

        chat_type = chat.get("type")
        chat_id = _int(chat.get("id"))
        if chat_type == "private":
            result = handle_private_message(self._session, message, city=self._city or None)
            self._session.commit()
            if result.reply is not None and chat_id is not None:
                await self._bot.send_message(chat_id, result.reply)
            return result.outcome

        if chat_id is None or str(chat_id) != self._discussion_group_id:
            return "ignored"
        return await self._moderate_group_message(message, chat_id)

    async def _moderate_group_message(self, message: dict[str, Any], chat_id: int) -> str:
        message_id = _int(message.get("message_id"))
        text_value = message.get("text")
        if message_id is None or not isinstance(text_value, str):
            return "ignored"

        existing = self._session.scalar(
            select(ModerationAction).where(
                ModerationAction.telegram_chat_id == chat_id,
                ModerationAction.telegram_message_id == message_id,
            )
        )
        if existing is not None:
            return f"moderation_{existing.action}"

        decision = evaluate_moderation(text_value)
        self._session.add(
            ModerationAction(
                city=self._city or None,
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                action=decision.action.value,
                reason=decision.audit_reason,
                model=None,
            )
        )
        # Persist the decision before a destructive Telegram call. If the network outcome is
        # uncertain, a replay of the same message will not issue a second delete.
        self._session.commit()

        if decision.action is RuleAction.DELETE:
            await self._bot.delete_message(chat_id, message_id)
        return f"moderation_{decision.action.value}"
