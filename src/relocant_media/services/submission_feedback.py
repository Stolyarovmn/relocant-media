"""Bounded, private status replies; never publish or assert an unverified tip."""
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.submission_queue import NEWS_KINDS
from relocant_media.storage.models import Submission

INCIDENT = re.compile(
    r"авари[яиюй]|дтп|затор|пробк|перекр[её]ст|проезд.{0,15}закрыт|проехать нельзя", re.I)
MESSAGES = {
    "missing_primary": "Спасибо за сообщение. "
        "Пока недостаточно подтверждённых фактов для публикации. "
        "Пришлите первоисточник или уточните, что, где и когда произошло и что вы видели лично. "
        "Можно ответить на это сообщение. Отсутствие подтверждения "
        "не означает, что сообщение ложное; "
        "до проверки оно останется в закрытой очереди.",
    "eyewitness_unverified": "Спасибо за сигнал. Пока это неподтверждённое сообщение очевидца. "
        "Уточните точный перекрёсток/адрес, направление, время наблюдения и что видно лично: "
        "весь проезд закрыт или движение затруднено? "
        "Фото/видео присылайте только если это безопасно; "
        "не снимайте за рулём и не раскрывайте лица пострадавших и личные данные. "
        "Ответьте на это сообщение. Одного сигнала недостаточно для автоматической публикации; "
        "без свежего подтверждения такой сигнал быстро утратит актуальность. "
        "Это редакционный ящик, не экстренная служба; мгновенную проверку не гарантируем.",
    "source_linked": "Спасибо. Найден доступный первоисточник; материал поставлен в приоритет "
        "редакционной проверки. Это ещё не подтверждение всех утверждений вашего сообщения "
        "и не обещание публикации: впереди проверка фактов, актуальности и дублей.",
    "already_considered": "Спасибо. Этот повод уже рассмотрен редакцией; повторно публиковать "
        "его не будем. Если вы заметили ошибку или появились новые обстоятельства, "
        "отправьте /correction со ссылкой на материал и подтверждением изменения.",
    "expired": "Сообщение не допущено к публикации: за время проверки не появилось достаточного "
        "подтверждения и истёк срок актуальности. Это не заключение о ложности сообщения. "
        "Если есть новые сведения, отправьте новую /tip с актуальным временем и источником.",
}


def resolve_submission(row: Submission, raw_ids: list[int], *, now: datetime) -> None:
    """Only public evidence may link a tip; private prose never becomes evidence."""
    created = row.created_at.replace(tzinfo=row.created_at.tzinfo or UTC)
    incident = bool(INCIDENT.search(str(row.payload.get("text", ""))))
    expired = now - created >= (timedelta(hours=1) if incident else timedelta(hours=48))
    if raw_ids:
        status, reason = "source_linked", "source_linked"
    elif expired:
        status, reason = "expired", "expired"
    else:
        status = "waiting_primary"
        reason = "eyewitness_unverified" if incident else "missing_primary"
    row.status = status
    row.payload = {**row.payload, "raw_item_ids": raw_ids, "triage_reason": reason,
                   "last_triaged_at": now.isoformat(), "eyewitness_report": incident}


def _block(session: Session, row: Submission, replies: dict[str, Any], reason: str) -> None:
    replies[reason] = {"status": "blocked", "attempted_at": datetime.now(UTC).isoformat()}
    row.payload = {**row.payload, "feedback": replies}
    session.commit()


async def reply_to_submissions(
    engine: Engine, bot: TelegramBotAPI, *, enabled: bool, limit: int = 20,
    only_ids: set[int] | None = None,
) -> dict[str, Any]:
    if not enabled:
        return dict(status="disabled", sent=0, uncertain=0, blocked=0)
    sent = uncertain = blocked = 0
    with Session(engine) as session:
        query = select(Submission.id).where(Submission.kind.in_(NEWS_KINDS),
            Submission.status.in_(("waiting_primary", "source_linked", "expired")),
            Submission.payload["city"].as_string() == "belgrade",
            Submission.payload["test_only"].as_boolean().is_not(True),
            or_(*(and_(Submission.payload["triage_reason"].as_string() == reason,
                       Submission.payload["feedback"][reason]["status"].as_string().is_(None))
                  for reason in MESSAGES))).order_by(Submission.id)
        if only_ids is not None:
            query = query.where(Submission.id.in_(only_ids))
        ids = list(session.scalars(query.limit(min(max(limit, 0), 20))))
    for sid in ids:
        with Session(engine) as session:
            row = session.get(Submission, sid)
            assert row is not None
            payload = row.payload
            reason = payload.get("triage_reason")
            if (payload.get("city") != "belgrade" or payload.get("test_only")
                    or reason not in MESSAGES):
                continue
            replies = dict(payload.get("feedback", {}))
            # Commit before network; sending/uncertain are never blindly retried.
            if reason in replies:
                continue
            cid = payload.get("chat_id")
            params: dict[str, Any] = {"chat_id": cid, "text": MESSAGES[reason],
                "protect_content": True, "allow_paid_broadcast": False,
                "link_preview_options": {"is_disabled": True}}
            if type(cid) is not int or type(row.telegram_message_id) is not int:
                _block(session, row, replies, reason)
                blocked += 1
                continue
            if cid > 0:
                if cid != row.telegram_user_id:
                    _block(session, row, replies, reason)
                    blocked += 1
                    continue
            else:
                if (payload.get("origin") != "channel_direct_messages"
                        or payload.get("channel_id") != APPROVED_CHANNEL_ID
                        or type(payload.get("topic_id")) is not int
                        or payload["topic_id"] <= 0):
                    _block(session, row, replies, reason)
                    blocked += 1
                    continue
                try:
                    chat = await bot._call("getChat", {"chat_id": cid})
                except (httpx.HTTPError, TelegramAPIError):
                    _block(session, row, replies, reason)
                    blocked += 1
                    continue
                if (not isinstance(chat, dict) or chat.get("is_direct_messages") is not True
                        or chat.get("parent_chat", {}).get("id") != APPROVED_CHANNEL_ID):
                    _block(session, row, replies, reason)
                    blocked += 1
                    continue
                params["direct_messages_topic_id"] = payload["topic_id"]
            params["reply_parameters"] = {"message_id": row.telegram_message_id}
            replies[reason] = {"status": "sending", "attempted_at": datetime.now(UTC).isoformat()}
            row.payload = {**payload, "feedback": replies}
            session.commit()
            try:
                result = await bot._call("sendMessage", params)
                if (not isinstance(result, dict) or type(result.get("message_id")) is not int
                        or result["message_id"] <= 0):
                    raise ValueError("invalid private response receipt")
            except Exception as error:
                # Even a timeout may have delivered. Never resend automatically.
                replies[reason] = {**replies[reason], "status": "uncertain",
                                   "error_type": type(error).__name__}
                uncertain += 1
            else:
                replies[reason] = {**replies[reason], "status": "sent",
                                   "telegram_message_id": result["message_id"]}
                sent += 1
            row.payload = {**row.payload, "feedback": replies}
            session.commit()
    with Session(engine) as session:
        attention = select(Submission.id).where(
            Submission.payload["city"].as_string() == "belgrade",
            or_(*(Submission.payload["feedback"][reason]["status"].as_string().in_(
                ("sending", "uncertain", "blocked")) for reason in MESSAGES)))
        unresolved = len(list(session.scalars(attention)))
    return dict(status="requires_review" if unresolved else "ready",
                sent=sent, uncertain=uncertain, blocked=blocked, unresolved=unresolved)


def save_requested_followup(session: Session, message: dict[str, Any]) -> bool:
    """A reply from the same author to our request starts a linked re-check."""
    reply_id = message.get("reply_to_message", {}).get("message_id")
    user_id = message.get("from", {}).get("id")
    cid = message.get("chat", {}).get("id")
    text = message.get("text")
    mid = message.get("message_id")
    if (type(reply_id) is not int or type(user_id) is not int or type(mid) is not int
            or type(cid) is not int or cid <= 0 or cid != user_id
            or not isinstance(text, str) or not text.strip() or len(text) > 10_000
            or text.startswith("/")):
        return False
    rows = session.scalars(select(Submission).where(Submission.telegram_user_id == user_id,
        Submission.status == "waiting_primary").order_by(Submission.id.desc()).limit(20))
    for prior in rows:
        if prior.payload.get("chat_id") != cid or prior.payload.get("city") != "belgrade":
            continue
        receipts = prior.payload.get("feedback", {}).values()
        if not any(r.get("status") == "sent" and r.get("telegram_message_id") == reply_id
                   for r in receipts):
            continue
        combined = str(prior.payload.get("text", "")) + "\nУточнение автора: " + text.strip()
        if len(combined) > 20_000:
            return False
        session.add(Submission(kind=prior.kind, telegram_user_id=user_id,
            telegram_message_id=mid, status="new", payload={"text": combined,
                "chat_id": cid, "city": "belgrade", "followup_to": prior.id}))
        prior.status = "superseded"
        return True
    return False
