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
from relocant_media.services.submission_reference import (
    PUBLIC_REFERENCE_PATTERN,
    new_submission_reference,
    submission_reference,
)
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
        "Редакционное партнёрство",
        "Предложите совместный материал, интервью или обмен информацией. "
        "Расскажите о проекте и приложите ссылку.",
    ),
    "ad_request": (
        "Платное размещение",
        "Для рекламы укажите проект, ссылку, желаемый формат и даты размещения.",
    ),
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
ROUTE_LABELS = {
    "Сотрудничество": "partnership",
    "Реклама": "ad_request",
    **{label: kind for kind, (label, _) in ROUTES.items()},
}
NEW_CASE_LABEL = "Новое обращение"
WELCOME_MENU = (
    "Напишите, что хотите сообщить. Можно отправить несколько сообщений и приложить фото или видео."
    "\n\nПерепроверьте детали, при необходимости дополните или исправьте сообщения, "
    "затем выберите действие ниже. Весь текст и вложения отправятся одним обращением."
)
GREETING = re.compile(
    r"(?:здравствуйте|здравствуй|привет|приветствую|доброе утро|добрый день|добрый вечер)"
    r"[\s.!?,…👋]*",
    re.IGNORECASE,
)
PRESENCE_CHECK = re.compile(
    r"(?:есть\s+кто(?:[- ]нибудь)?|кто(?:[- ]нибудь)?\s+(?:есть|тут|здесь)|"
    r"(?:вы|ты)\s+(?:тут|здесь)|алло|ау)[\s.!?,…]*", re.IGNORECASE,
)


def _start_text(text: str) -> bool:
    return (text == NEW_CASE_LABEL or text.partition(" ")[0].split("@")[0] == "/start"
            or GREETING.fullmatch(text) is not None
            or PRESENCE_CHECK.fullmatch(text) is not None)


def _notice_covers_input(notice: dict[str, Any], received_at: datetime) -> bool:
    if notice.get("status") in {"pending", "sending", "uncertain"}:
        return True
    sent_at = notice.get("sent_at")
    return (notice.get("status") == "sent" and isinstance(sent_at, str)
            and received_at <= datetime.fromisoformat(sent_at))


def _needs_start_notice(row: Submission, stage: str, received_at: datetime) -> bool:
    notices = [notice for key, notice in row.payload.get("reader_notices", {}).items()
               if key.split(":", maxsplit=1)[0] == stage]
    if not notices:
        return True
    latest = max(notices, key=lambda notice: notice["received_at"])
    # These messages were written while the reader was still waiting for this answer.
    return not _notice_covers_input(latest, received_at)


def _supersede_pending_prompts(row: Submission) -> None:
    for key, notice in row.payload.get("reader_notices", {}).items():
        if (key.split(":", maxsplit=1)[0] in {"menu", "prompt", "choice"}
                and notice["status"] == "pending"):
            _save_notice(row, key, status="superseded", superseded_at=_now().isoformat())


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


def _start_keyboard(row: Submission) -> list[list[dict[str, str]]]:
    return [[_button(row, NEW_CASE_LABEL, "new")]]


def _notice(
    row: Submission,
    key: str,
    text: str,
    *,
    reply_to: int | None = None,
    keyboard: list[list[dict[str, str]]] | None = None,
    received_at: datetime | None = None,
    submit_on_pick: bool = False,
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
    if submit_on_pick:
        notices[key]["submit_on_pick"] = True
    row.payload = {**row.payload, "reader_notices": notices, "reader_notice_pending": True}


def _menu(row: Submission, *, key: str, message_id: int | None, received_at: datetime) -> None:
    reference = submission_reference(row)
    buttons = [_button(row, label, f"pick:{kind}") for kind, (label, _) in ROUTES.items()]
    buttons.append(_button(row, f"Отменить №{reference}", "cancel"))
    keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    _notice(
        row,
        key,
        WELCOME_MENU,
        reply_to=message_id,
        keyboard=keyboard,
        received_at=received_at,
        submit_on_pick=True,
    )


def _prompt(row: Submission, *, key: str, received_at: datetime | None = None) -> None:
    reference = submission_reference(row)
    label, instruction = ROUTES[row.kind]
    _notice(
        row,
        key,
        f"{label}\n\n{instruction}\n\n"
        f"Когда всё пришлёте, нажмите «Отправить №{reference}».",
        keyboard=[
            [_button(row, f"Отправить №{reference}", "submit")],
            [
                _button(row, f"Сменить тему №{reference}", "menu"),
                _button(row, f"Отменить №{reference}", "cancel"),
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
    channel_id: int,
) -> Submission:
    payload: dict[str, Any] = {
        "city": "belgrade",
        "reader_intake": True,
        "public_reference": new_submission_reference(session),
        "chat_id": chat_id,
        "received_at": received_at.isoformat(),
        "text": "",
        "attachments": [],
    }
    if topic_id is not None:
        payload.update(
            origin="channel_direct_messages", channel_id=channel_id, topic_id=topic_id
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
    text = "\n".join(part["text"] for part in ordered if part["text"] and not (
        not part["attachments"] and _start_text(part["text"])))
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


def _source_notice(row: Submission, action: str) -> dict[str, Any]:
    stage = "prompt"
    if action == "new":
        stage = "accepted"
    elif action.startswith("pick:") or (action == "cancel" and row.status == "choosing"):
        stage = "menu"
    notices = row.payload.get("reader_notices", {})
    return max(
        (notice for key, notice in notices.items() if key.split(":", maxsplit=1)[0] == stage),
        key=lambda notice: (notice["received_at"], notice.get("telegram_message_id") or 0),
        default={},
    )


def _source_notice_id(row: Submission, action: str) -> int | None:
    latest = _source_notice(row, action)
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
    sender: tuple[int, int, int | None],
    message_id: int,
    received_at: datetime,
) -> bool:
    draft = next((case for case in cases if case.status in DRAFT_STATES), None)
    if match := re.fullmatch(
        rf"(Отправить|Сменить тему|Отменить) №({PUBLIC_REFERENCE_PATTERN}|[0-9]{{1,18}})", label,
    ):
        row = draft if draft is not None and submission_reference(draft) == match[2] else None
        action = NUMBERED_ACTIONS[match[1]]
        if action == "cancel" and draft is None:
            _clear_cancelled_keyboard(session, sender, match[2], message_id, received_at)
    else:
        kind = ROUTE_LABELS.get(label)
        if kind is None:
            return False
        if draft is None:
            # Repeated taps in one poll can arrive before Telegram shows the new keyboard.
            return any(
                case.kind == kind
                and _notice_covers_input(
                    case.payload.get("reader_notices", {}).get("accepted", {}), received_at)
                for case in cases
            )
        if draft.status == "collecting":
            return draft.kind == kind
        row, action = draft, f"pick:{kind}"
    source_notice = _source_notice(row, action) if row is not None else {}
    pending_choice = (action.startswith("pick:") and source_notice.get("status") == "pending"
                      and source_notice.get("submit_on_pick") is True)
    if row is not None and (_source_notice_id(row, action) is not None or pending_choice):
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


def _clear_cancelled_keyboard(
    session: Session, sender: tuple[int, int, int | None], reference: str,
    message_id: int, received_at: datetime,
) -> None:
    user_id, chat_id, topic_id = sender
    row = session.scalar(select(Submission).where(
        Submission.telegram_user_id == user_id,
        Submission.payload["reader_intake"].as_boolean().is_(True),
        cast(Submission.payload["chat_id"].as_string(), BigInteger) == chat_id,
    ).order_by(Submission.id.desc()).limit(1))
    if (row is None or row.status != "cancelled" or row.payload.get("topic_id") != topic_id
            or submission_reference(row) != reference):
        return
    cancelled = row.payload.get("reader_notices", {}).get("cancelled", {})
    if (cancelled.get("status") == "sent" and not cancelled.get("remove_keyboard")
            and not cancelled.get("keyboard")):
        _notice(
            row, "cancelled_keyboard", f"Черновик №{reference} уже отменён.",
            reply_to=message_id, received_at=received_at, keyboard=_start_keyboard(row),
        )


def record_reader_message(
    session: Session,
    message: dict[str, Any],
    *,
    edited: bool = False,
    channel_id: int = APPROVED_CHANNEL_ID,
    transport_notice: dict[str, Any] | None = None,
) -> bool:
    identity = _identity(message)
    items = _items(message)
    if identity is None or items is None:
        return False
    user_id, chat_id, message_id, topic_id = identity
    text = items["text"]
    start = not items["attachments"] and _start_text(text)
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
            sender=(user_id, chat_id, topic_id),
            message_id=message_id,
            received_at=received_at,
        )
    ):
        return True
    if not items["attachments"] and text in ROUTE_LABELS and not any(
        case.status in DRAFT_STATES for case in cases
    ):
        start = True
    reply = message.get("reply_to_message") or {}
    reply_id = None if start else reply.get("message_id")
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
            session, user_id, chat_id, topic_id,
            message_id=message_id, received_at=received_at, channel_id=channel_id,
        )
        _menu(row, key="menu", message_id=message_id, received_at=received_at)
    elif (start and row.status == "choosing"
          and _needs_start_notice(row, "menu", received_at)):
        _menu(row, key=f"menu:{message_id}", message_id=message_id, received_at=received_at)
    elif (start and row.status == "collecting"
          and _needs_start_notice(row, "prompt", received_at)):
        _prompt(row, key=f"prompt:{message_id}", received_at=received_at)
    if start:
        return True
    if not _append(row, message_id, items):
        _notice(
            row,
            f"limit:{message_id}",
            f"№{submission_reference(row)} · В заявке помещается до 24 вложений "
            "и 20 000 знаков. Для остального создайте новое обращение.",
            reply_to=message_id,
        )
        return True
    if row.status == "waiting_primary":
        row.status = "new"
        # Recheck with the added evidence and preserve earlier delivery receipts.
        row.payload = {**row.payload, "last_followup_at": received_at.isoformat()}
    if row.status not in DRAFT_STATES:
        if transport_notice and transport_notice.get("key", "").startswith("received:"):
            # The ingress receipt and attachment commit together. Its message ID
            # also lets a reply to the fast acknowledgement reach this same case.
            notices = {**row.payload.get("reader_notices", {}),
                       f"added:{message_id}": dict(transport_notice)}
            row.payload = {**row.payload, "reader_notices": notices}
        else:
            _notice(
                row,
                f"added:{message_id}",
                f"Добавлено к заявке №{submission_reference(row)}.",
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
    reference = submission_reference(row)
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
            channel_id=row.payload.get("channel_id", APPROVED_CHANNEL_ID),
        )
        _menu(new_case, key="menu", message_id=None, received_at=received_at)
        return "Открываю новое обращение."
    if row.status not in DRAFT_STATES:
        return f"Заявка №{reference} уже отправлена."
    if action == "cancel":
        row.status = "cancelled"
        _supersede_pending_prompts(row)
        _notice(
            row,
            "cancelled",
            f"Черновик №{reference} отменён.",
            reply_to=message_id,
            received_at=received_at,
            keyboard=_start_keyboard(row),
        )
        return "Черновик отменён."
    notices = row.payload.get("reader_notices", {})
    route = action.removeprefix("pick:")
    if action.startswith("pick:") and route in ROUTES and row.status == "choosing":
        if not _source_notice(row, action).get("submit_on_pick"):
            row.kind = route
            row.status = "collecting"
            _prompt(row, key=f"prompt:{len(notices)}", received_at=received_at)
            return f"Выбрано: {ROUTES[route][0]}."
        if not _has_submission_content(row, route):
            return ROUTES[route][1] + "\n\nЗатем выберите действие ниже."
        row.kind = route
    elif action == "menu" and row.status == "collecting":
        row.status = "choosing"
        _menu(row, key=f"menu:{len(notices)}", message_id=None, received_at=received_at)
        return "Выберите тему."
    elif action != "submit" or row.status != "collecting":
        return "Используйте последнее сообщение бота."
    elif not _has_submission_content(row, row.kind):
        return "Добавьте описание или вложение, затем отправьте заявку."
    accepted_at = received_at
    row.status = "new"
    _supersede_pending_prompts(row)
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
        f"Ваше обращение №{reference} отправлено в работу.\n\n"
        "Здесь сообщим результат. "
        "Фото, видео или уточнение можно дослать ответом на это сообщение.",
        keyboard=_start_keyboard(row),
        received_at=accepted_at,
    )
    return f"Обращение №{reference} принято."


def _has_submission_content(row: Submission, kind: str) -> bool:
    text = row.payload["text"].strip()
    return bool((text and not _start_text(text))
                or (kind == "news_tip" and row.payload["attachments"]))


async def reader_destination(
    bot: TelegramBotAPI, row: Submission, *, channel_id: int = APPROVED_CHANNEL_ID,
) -> dict[str, Any] | None:
    payload = row.payload
    cid = payload.get("chat_id")
    if type(cid) is not int:
        return None
    if cid > 0:
        return {"chat_id": cid} if cid == row.telegram_user_id else None
    topic_id = payload.get("topic_id")
    if (
        payload.get("origin") != "channel_direct_messages"
        or payload.get("channel_id") != channel_id
        or type(topic_id) is not int
        or topic_id <= 0
    ):
        return None
    chat = await bot._call("getChat", {"chat_id": cid})
    if (
        not isinstance(chat, dict)
        or chat.get("is_direct_messages") is not True
        or (chat.get("parent_chat") or {}).get("id") != channel_id
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
    engine: Engine, bot: TelegramBotAPI, *, limit: int = 30,
    channel_id: int = APPROVED_CHANNEL_ID,
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
                    destination = await reader_destination(bot, row, channel_id=channel_id)
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
                    keyboard = [
                        [{"text": button["text"]} for button in line]
                        for line in item["keyboard"]
                    ]
                    start_keyboard = keyboard == [[{"text": NEW_CASE_LABEL}]]
                    persistent = start_keyboard or item.get("submit_on_pick") is True
                    params["reply_markup"] = {
                        "keyboard": keyboard,
                        "resize_keyboard": True,
                        "one_time_keyboard": not persistent,
                    }
                    if persistent:
                        params["reply_markup"]["is_persistent"] = True
                elif item.get("remove_keyboard"):
                    params["reply_markup"] = {"remove_keyboard": True}
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
