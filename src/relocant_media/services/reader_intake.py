"""Shared, private Telegram submission form and its early replies."""

import re
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import BigInteger, cast, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.storage.models import AdRequest, Submission

ROUTES = {
    "news_tip": (
        "Предложить новость",
        "Что, где и когда произошло? Можно добавить ссылку, фото или видео.",
    ),
    "complaint": (
        "Пожаловаться",
        "На что жалоба? Пришлите ссылку на пост или комментарий и опишите проблему.",
    ),
    "correction": (
        "Сообщить об ошибке",
        "Пришлите ссылку на наш пост, ошибочный фрагмент и верные сведения.",
    ),
    "partnership": (
        "Сотрудничество",
        "Расскажите о проекте, приложите ссылку и опишите предложение.",
    ),
    "ad_request": ("Реклама", "Пришлите ссылку на проект, формат размещения и желаемые даты."),
    "technical": (
        "Карта или бот",
        "Опишите проблему. Пришлите ссылку, модель устройства и шаг, на котором она возникла.",
    ),
    "question": ("Вопрос или идея", "Напишите вопрос или предложение для редакции."),
}
CB_PREFIX = "intake:"
DRAFT_STATES = ("choosing", "collecting")
OPEN_STATES = (*DRAFT_STATES, "new", "waiting_primary", "source_linked")
MEDIA_TYPES = ("photo", "video", "document", "voice")
NUMBERED_ACTIONS = {"Отправить": "submit", "Сменить тему": "menu", "Отменить": "cancel"}
NEW_CASE_LABEL = "Новое обращение"


def _now() -> datetime:
    return datetime.now(UTC)


def _date(message: dict[str, Any]) -> datetime:
    epoch = message.get("date")
    now = _now()
    if type(epoch) is int and 0 < epoch <= now.timestamp() + 60:
        return min(datetime.fromtimestamp(epoch, UTC), now)
    return now


def _identity(message: dict[str, Any]) -> tuple[int, int, int, int | None] | None:
    sender = message.get("from", {})
    chat = message.get("chat", {})
    user_id = sender.get("id")
    chat_id = chat.get("id")
    message_id = message.get("message_id")
    if (
        type(user_id) is not int
        or user_id <= 0
        or sender.get("is_bot")
        or type(chat_id) is not int
        or type(message_id) is not int
        or message_id <= 0
    ):
        return None
    if chat.get("type") == "private" and chat_id == user_id:
        return user_id, chat_id, message_id, None
    topic_id = message.get("direct_messages_topic", {}).get("topic_id")
    if (
        chat.get("is_direct_messages") is True
        and chat_id < 0
        and type(topic_id) is int
        and topic_id > 0
    ):
        return user_id, chat_id, message_id, topic_id
    return None


def _button(row: Submission, label: str, action: str) -> dict[str, str]:
    return {"text": label, "callback_data": f"{CB_PREFIX}{row.id}:{action}"}


def _notice(
    row: Submission,
    key: str,
    text: str,
    *,
    reply_to: int | None = None,
    keyboard: list[list[dict[str, str]]] | None = None,
    received_at: datetime | None = None,
) -> None:
    notices = dict(row.payload.get("reader_notices", {}))
    if key in notices:
        return
    notices[key] = {
        "status": "pending",
        "text": text,
        "keyboard": keyboard,
        "reply_to": reply_to,
        "received_at": (received_at or _now()).isoformat(),
    }
    row.payload = {**row.payload, "reader_notices": notices, "reader_notice_pending": True}


def _menu(row: Submission, *, key: str, message_id: int | None, received_at: datetime) -> None:
    buttons = [_button(row, label, f"pick:{kind}") for kind, (label, _) in ROUTES.items()]
    buttons.append(_button(row, f"Отменить №{row.id}", "cancel"))
    keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    _notice(
        row,
        key,
        f"Обращение №{row.id}. Выберите тему на клавиатуре.",
        reply_to=message_id,
        keyboard=keyboard,
        received_at=received_at,
    )


def _prompt(row: Submission, *, key: str, received_at: datetime | None = None) -> None:
    label, instruction = ROUTES[row.kind]
    _notice(
        row,
        key,
        f"№{row.id} · {label}\n\n{instruction}\n\n"
        f"Когда всё пришлёте, нажмите «Отправить №{row.id}».",
        keyboard=[
            [_button(row, f"Отправить №{row.id}", "submit")],
            [
                _button(row, f"Сменить тему №{row.id}", "menu"),
                _button(row, f"Отменить №{row.id}", "cancel"),
            ],
        ],
        received_at=received_at,
    )


def _new_case(
    session: Session,
    user_id: int,
    chat_id: int,
    topic_id: int | None,
    *,
    message_id: int,
    received_at: datetime,
) -> Submission:
    payload: dict[str, Any] = {
        "city": "belgrade",
        "reader_intake": True,
        "chat_id": chat_id,
        "received_at": received_at.isoformat(),
        "text": "",
        "attachments": [],
    }
    if topic_id is not None:
        payload.update(
            origin="channel_direct_messages", channel_id=APPROVED_CHANNEL_ID, topic_id=topic_id
        )
    row = Submission(
        kind="reader_unclassified",
        telegram_user_id=user_id,
        telegram_message_id=message_id,
        payload=payload,
        status="choosing",
    )
    session.add(row)
    session.flush()
    return row


def _items(message: dict[str, Any]) -> dict[str, Any] | None:
    body = message.get("text", message.get("caption", ""))
    if not isinstance(body, str):
        return None
    attachments: list[dict[str, Any]] = []
    for kind in MEDIA_TYPES:
        item = message.get(kind)
        if kind == "photo" and isinstance(item, list) and item:
            item = item[-1]
        if isinstance(item, dict) and isinstance(item.get("file_id"), str):
            attachments.append(
                {
                    "kind": kind,
                    "file_id": item["file_id"],
                    "file_unique_id": item.get("file_unique_id"),
                }
            )
    return {"text": body.strip(), "attachments": attachments}


def _append(row: Submission, message_id: int, items: dict[str, Any]) -> bool:
    parts = dict(row.payload.get("reader_parts", {}))
    if len(parts) >= 32 and str(message_id) not in parts:
        return False
    parts[str(message_id)] = items
    ordered = [parts[key] for key in sorted(parts, key=int)]
    text = "\n".join(part["text"] for part in ordered if part["text"])
    attachments: dict[str, dict[str, Any]] = {}
    for part in ordered:
        for media in part["attachments"]:
            attachments[media.get("file_unique_id") or media["file_id"]] = media
    if len(text) > 20_000 or len(attachments) > 24:
        return False
    row.payload = {
        **row.payload,
        "reader_parts": parts,
        "text": text,
        "attachments": list(attachments.values()),
        "media_present": bool(attachments),
        "updated_at": _now().isoformat(),
    }
    return True


def _message_ids(row: Submission) -> set[int]:
    receipts = (
        *row.payload.get("reader_notices", {}).values(),
        *row.payload.get("feedback", {}).values(),
    )
    return {
        receipt["telegram_message_id"]
        for receipt in receipts
        if receipt.get("status") == "sent" and type(receipt.get("telegram_message_id")) is int
    }


def _source_notice_id(row: Submission, action: str) -> int | None:
    stage = "prompt"
    if action == "new":
        stage = "accepted"
    elif action.startswith("pick:") or (action == "cancel" and row.status == "choosing"):
        stage = "menu"
    notices = row.payload.get("reader_notices", {})
    latest: dict[str, Any] = max(
        (notice for key, notice in notices.items() if key.split(":", maxsplit=1)[0] == stage),
        key=lambda notice: notice["received_at"],
        default={},
    )
    message_id = latest.get("telegram_message_id")
    return message_id if latest.get("status") == "sent" and type(message_id) is int else None


def _recent_cases(
    session: Session, user_id: int, chat_id: int, topic_id: int | None
) -> list[Submission]:
    query = (
        select(Submission)
        .where(
            Submission.telegram_user_id == user_id,
            Submission.status.in_(OPEN_STATES),
            Submission.payload["reader_intake"].as_boolean().is_(True),
            cast(Submission.payload["chat_id"].as_string(), BigInteger) == chat_id,
        )
        .order_by(Submission.id.desc())
        .limit(30)
    )
    return [row for row in session.scalars(query) if row.payload.get("topic_id") == topic_id]


def _record_keyboard_choice(
    session: Session,
    cases: list[Submission],
    label: str,
    *,
    message_id: int,
    received_at: datetime,
) -> bool:
    draft = next((case for case in cases if case.status in DRAFT_STATES), None)
    if match := re.fullmatch(r"(Отправить|Сменить тему|Отменить) №([0-9]{1,18})", label):
        row = draft if draft is not None and draft.id == int(match[2]) else None
        action = NUMBERED_ACTIONS[match[1]]
    else:
        kind = next((kind for kind, (caption, _) in ROUTES.items() if label == caption), None)
        if kind is None or draft is None:
            return False
        if draft.status == "collecting":
            return draft.kind == kind
        row, action = draft, f"pick:{kind}"
    if row is not None and _source_notice_id(row, action) is not None:
        notice_count = len(row.payload.get("reader_notices", {}))
        answer = _apply_action(
            session,
            row,
            action,
            message_id=message_id,
            received_at=received_at,
        )
        if len(row.payload.get("reader_notices", {})) == notice_count:
            _notice(
                row, f"choice:{message_id}", answer, reply_to=message_id, received_at=received_at
            )
    return True


def record_reader_message(
    session: Session, message: dict[str, Any], *, edited: bool = False
) -> bool:
    identity = _identity(message)
    items = _items(message)
    if identity is None or items is None:
        return False
    user_id, chat_id, message_id, topic_id = identity
    start = not items["attachments"] and (
        items["text"] == NEW_CASE_LABEL or items["text"].partition(" ")[0].split("@")[0] == "/start"
    )
    if not (items["text"] or items["attachments"]):
        return False
    received_at = _date(message)
    cases = _recent_cases(session, user_id, chat_id, topic_id)
    if edited:
        row = next(
            (case for case in cases if str(message_id) in case.payload.get("reader_parts", {})),
            None,
        )
        return bool(row and _append(row, message_id, items))
    if (
        not start
        and not items["attachments"]
        and _record_keyboard_choice(
            session,
            cases,
            items["text"],
            message_id=message_id,
            received_at=received_at,
        )
    ):
        return True
    reply = message.get("reply_to_message") or {}
    reply_id = reply.get("message_id")
    row = next((case for case in cases if reply_id in _message_ids(case)), None)
    if row is None and reply_id is None:
        row = next((case for case in cases if case.status in DRAFT_STATES), None)
        if row is None and items["attachments"] and cases:
            candidate = cases[0]
            accepted = candidate.payload.get("accepted_at")
            if accepted and (_now() - datetime.fromisoformat(accepted)).total_seconds() <= 1800:
                row = candidate
    if row is None:
        row = _new_case(
            session, user_id, chat_id, topic_id, message_id=message_id, received_at=received_at
        )
        _menu(row, key="menu", message_id=message_id, received_at=received_at)
    elif start and row.status == "choosing":
        _menu(row, key=f"menu:{message_id}", message_id=message_id, received_at=received_at)
    elif start and row.status == "collecting":
        _prompt(row, key=f"prompt:{message_id}")
    if start:
        return True
    if not _append(row, message_id, items):
        _notice(
            row,
            f"limit:{message_id}",
            f"№{row.id} · В заявке помещается до 24 вложений "
            "и 20 000 знаков. Для остального создайте новое обращение.",
            reply_to=message_id,
        )
        return True
    if row.status == "waiting_primary":
        row.status = "new"
        # Recheck with the added evidence and preserve earlier delivery receipts.
        row.payload = {**row.payload, "last_followup_at": received_at.isoformat()}
    if row.status not in DRAFT_STATES:
        _notice(
            row,
            f"added:{message_id}",
            f"Добавлено к заявке №{row.id}.",
            reply_to=message_id,
            received_at=received_at,
        )
    return True


def record_reader_callback(session: Session, callback: object) -> str | None:
    if not isinstance(callback, dict) or not isinstance(callback.get("data"), str):
        return None
    data = callback["data"]
    if not data.startswith(CB_PREFIX):
        return None
    number, sep, action = data[len(CB_PREFIX) :].partition(":")
    if not sep or not number.isascii() or not number.isdigit() or len(number) > 18:
        return "Эта кнопка устарела."
    row = session.get(Submission, int(number))
    message = callback.get("message") or {}
    sender = callback.get("from") or {}
    chat = message.get("chat") or {}
    source_message_id = message.get("message_id")
    topic_id = (message.get("direct_messages_topic") or {}).get("topic_id")
    if (
        row is None
        or row.payload.get("reader_intake") is not True
        or sender.get("id") != row.telegram_user_id
        or sender.get("is_bot")
        or chat.get("id") != row.payload.get("chat_id")
        or (topic_id is not None and topic_id != row.payload.get("topic_id"))
        or type(source_message_id) is not int
        or source_message_id != _source_notice_id(row, action)
    ):
        return "Эта кнопка не относится к вашему обращению."
    return _apply_action(session, row, action, message_id=source_message_id, received_at=_now())


def _apply_action(
    session: Session,
    row: Submission,
    action: str,
    *,
    message_id: int,
    received_at: datetime,
) -> str:
    if action == "new":
        assert row.telegram_user_id is not None
        chat_id = row.payload["chat_id"]
        topic_id = row.payload.get("topic_id")
        existing = _recent_cases(session, row.telegram_user_id, chat_id, topic_id)
        if any(case.status in DRAFT_STATES for case in existing):
            return "У вас уже есть черновик. Продолжите его в сообщениях выше."
        new_case = _new_case(
            session,
            row.telegram_user_id,
            chat_id,
            topic_id,
            message_id=message_id,
            received_at=received_at,
        )
        _menu(new_case, key="menu", message_id=None, received_at=received_at)
        return "Открываю новое обращение."
    if row.status not in DRAFT_STATES:
        return f"Заявка №{row.id} уже отправлена."
    if action == "cancel":
        row.status = "cancelled"
        _notice(
            row,
            "cancelled",
            f"Черновик №{row.id} отменён. Новое обращение можно начать сообщением.",
        )
        return "Черновик отменён."
    notices = row.payload.get("reader_notices", {})
    route = action.removeprefix("pick:")
    if action.startswith("pick:") and route in ROUTES and row.status == "choosing":
        row.kind = route
        row.status = "collecting"
        _prompt(row, key=f"prompt:{len(notices)}", received_at=received_at)
        return f"Выбрано: {ROUTES[route][0]}."
    if action == "menu" and row.status == "collecting":
        row.status = "choosing"
        _menu(row, key=f"menu:{len(notices)}", message_id=None, received_at=received_at)
        return "Выберите тему."
    if action != "submit" or row.status != "collecting":
        return "Используйте последнее сообщение бота."
    if not row.payload["text"] and not (row.kind == "news_tip" and row.payload["attachments"]):
        return "Добавьте описание или вложение, затем отправьте заявку."
    accepted_at = received_at
    row.status = "new"
    row.payload = {**row.payload, "accepted_at": accepted_at.isoformat()}
    # The first message can be /start, so reply to the actual description when present.
    input_ids = row.payload.get("reader_parts", {})
    if input_ids:
        row.telegram_message_id = max(map(int, input_ids))
    if row.kind == "ad_request":
        session.add(
            AdRequest(
                telegram_user_id=row.telegram_user_id,
                payload={**row.payload, "submission_id": row.id},
                status="new",
            )
        )
    _notice(
        row,
        "accepted",
        f"Заявка №{row.id} принята. Здесь сообщим результат. "
        "Чтобы добавить к ней фото, видео или уточнение, ответьте на это сообщение.",
        keyboard=[[_button(row, NEW_CASE_LABEL, "new")]],
        received_at=accepted_at,
    )
    return f"Заявка №{row.id} принята."


async def reader_destination(bot: TelegramBotAPI, row: Submission) -> dict[str, Any] | None:
    payload = row.payload
    cid = payload.get("chat_id")
    if type(cid) is not int:
        return None
    if cid > 0:
        return {"chat_id": cid} if cid == row.telegram_user_id else None
    topic_id = payload.get("topic_id")
    if (
        payload.get("origin") != "channel_direct_messages"
        or payload.get("channel_id") != APPROVED_CHANNEL_ID
        or type(topic_id) is not int
        or topic_id <= 0
    ):
        return None
    chat = await bot._call("getChat", {"chat_id": cid})
    if (
        not isinstance(chat, dict)
        or chat.get("is_direct_messages") is not True
        or (chat.get("parent_chat") or {}).get("id") != APPROVED_CHANNEL_ID
    ):
        return None
    return {"chat_id": cid, "direct_messages_topic_id": topic_id}


def _save_notice(row: Submission, key: str, **fields: Any) -> None:
    notices = dict(row.payload["reader_notices"])
    notices[key] = {**notices[key], **fields}
    row.payload = {
        **row.payload,
        "reader_notices": notices,
        "reader_notice_pending": any(item["status"] == "pending" for item in notices.values()),
    }


async def deliver_reader_notices(
    engine: Engine, bot: TelegramBotAPI, *, limit: int = 30
) -> dict[str, Any]:
    stats: dict[str, Any] = {"sent": 0, "uncertain": 0, "blocked": 0, "response_seconds": {}}
    with Session(engine) as session:
        rows = session.scalars(
            select(Submission)
            .where(
                Submission.payload["reader_notice_pending"].as_boolean().is_(True),
            )
            .order_by(Submission.id)
            .limit(limit)
        ).all()
        for row in rows:
            for key, item in row.payload["reader_notices"].items():
                if item["status"] != "pending":
                    continue
                if sum(stats[k] for k in ("sent", "uncertain", "blocked")) >= limit:
                    return stats
                try:
                    destination = await reader_destination(bot, row)
                except (httpx.HTTPError, TelegramAPIError):
                    # No send was attempted. Keep this notice queued for the next pass.
                    break
                if not destination:
                    _save_notice(row, key, status="blocked")
                    session.commit()
                    stats["blocked"] += 1
                    continue
                params: dict[str, Any] = {
                    **destination,
                    "text": item["text"],
                    "protect_content": True,
                    "allow_paid_broadcast": False,
                    "link_preview_options": {"is_disabled": True},
                }
                if item["keyboard"]:
                    params["reply_markup"] = {
                        "keyboard": [
                            [{"text": button["text"]} for button in line]
                            for line in item["keyboard"]
                        ],
                        "resize_keyboard": True,
                        "one_time_keyboard": True,
                    }
                if item["reply_to"] is not None:
                    params["reply_parameters"] = {
                        "message_id": item["reply_to"],
                        "allow_sending_without_reply": True,
                    }
                _save_notice(row, key, status="sending", attempted_at=_now().isoformat())
                session.commit()
                try:
                    result = await bot._call("sendMessage", params)
                    mid = TelegramBotAPI._message_id(result)
                except Exception as error:
                    _save_notice(row, key, status="uncertain", error_type=type(error).__name__)
                    stats["uncertain"] += 1
                else:
                    sent_at = _now()
                    delay = max(
                        0,
                        round(
                            (sent_at - datetime.fromisoformat(item["received_at"])).total_seconds(),
                            3,
                        ),
                    )
                    _save_notice(
                        row,
                        key,
                        status="sent",
                        telegram_message_id=mid,
                        sent_at=sent_at.isoformat(),
                        response_seconds=delay,
                    )
                    stage = key.split(":", maxsplit=1)[0]
                    stats["response_seconds"].setdefault(stage, []).append(delay)
                    stats["sent"] += 1
                session.commit()
    return stats
