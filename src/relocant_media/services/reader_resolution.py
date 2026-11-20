"""Close reader submissions and deliver the outcome to their verified private chat."""

import re
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

import httpx
from sqlalchemy import BigInteger, and_, cast, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.domain.dedup import EventCandidate, same_event
from relocant_media.services.owner_review import OwnerUpdateResult
from relocant_media.services.reader_intake import reader_destination
from relocant_media.services.reader_publication import (
    human_review_reason,
    material_fingerprint,
    owner_publication_summary,
)
from relocant_media.services.reader_source_discovery import (
    DISCOVERY_FIELD,
    confirmed_source_signal,
    discovery_deadline_reached,
)
from relocant_media.services.submission_feedback import INCIDENT
from relocant_media.services.submission_queue import tip_urls
from relocant_media.services.submission_reference import submission_reference
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission

_OWNER_KINDS = {
    "correction": "Исправление нашего поста",
    "complaint": "Жалоба",
    "ad_request": "Платное размещение",
    "partnership": "Редакционное партнёрство",
    "technical": "Проблема с картой или ботом",
    "question": "Вопрос или предложение",
}
_OWNER_ACTION = re.compile(
    r"rr:(close|media|approve_media|reject_media|primary):([1-9][0-9]{0,17})",
)
_MEDIA_METHODS = {"photo": "sendPhoto", "video": "sendVideo",
                  "document": "sendDocument", "voice": "sendVoice", "audio": "sendAudio"}
_NO_ACTION = "Редакция рассмотрела ваше обращение. Дальнейших действий по нему не планируется."
_EXPIRED = (
    "Обращение закрыто. За время проверки мы не нашли достаточного подтверждения, "
    "а срок актуальности истёк. Если появились новые сведения, пришлите новое обращение."
)
_NOT_PUBLISHED = "Редакция рассмотрела новость и решила не публиковать её."
_NO_PUBLIC_CONFIRMATION = (
    "За время проверки мы не нашли достаточного публичного подтверждения, поэтому новость "
    "по этому обращению не вышла. Если появится объявление организатора или другие "
    "проверяемые сведения, пришлите их в новом обращении."
)


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _when(payload: dict[str, Any], key: str) -> datetime | None:
    raw = payload.get(key)
    if not isinstance(raw, str):
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _elapsed(payload: dict[str, Any], key: str, now: datetime) -> float | None:
    stamp = _when(payload, key)
    return max(0, round((now - stamp).total_seconds(), 3)) if stamp else None


def _resolution(row: Submission) -> dict[str, Any]:
    return dict(row.payload.get("reader_resolution", {}))


def _set_resolution(row: Submission, **updates: Any) -> None:
    row.payload = {**row.payload, "reader_resolution": {**_resolution(row), **updates}}


def _close(row: Submission, text: str, outcome: str, *, now: datetime) -> None:
    row.status = "closed"
    _set_resolution(
        row, final={"status": "pending", "text": text},
        outcome=outcome, decided_at=now.isoformat(),
    )


def _confirmed_post(session: Session, run: EditorialRun, city: str) -> int | None:
    if run.delivery_status != "sent" or not run.post_id:
        return None
    post = session.get(Post, run.post_id)
    if (post is not None and post.city == city and post.published_at
            and _positive(post.telegram_message_id)):
        return post.telegram_message_id
    return None


def _candidate(item: RawItem) -> EventCandidate:
    return EventCandidate(
        raw_item_id=item.id, source_id=item.source_id, url=item.url, title=item.title,
        body=item.body, published_at=item.published_at, canonical_url=item.canonical_url,
        content_sha256=item.content_sha256,
    )


def _related_runs(
    session: Session, raw_ids: set[int], direct_runs: list[EditorialRun], *, city: str,
) -> list[EditorialRun]:
    duplicate_ids = {
        value for run in direct_runs if _positive(value := run.outcome.get("duplicate_of_raw_item"))
    } - raw_ids
    repeated_posts = {
        int(match.group(1)) for run in direct_runs
        if (match := re.fullmatch(
            r"draft_repeats_recent_post:([0-9]+)", str(run.outcome.get("reason", "")),
        )) is not None
    }
    candidates = select(EditorialRun).join(RawItem).join(Source).where(Source.city == city)
    if duplicate_ids or repeated_posts:
        return list(session.scalars(candidates.where(
            or_(
                EditorialRun.raw_item_id.in_(duplicate_ids),
                EditorialRun.post_id.in_(repeated_posts),
            ),
        )))
    if len(direct_runs) == len(raw_ids):
        return []
    # The batch deliberately skips source versions classified as the same event.
    # Match those versions against confirmed posts using that identical event rule.
    without_run = raw_ids - {run.raw_item_id for run in direct_runs}
    unmatched = [
        _candidate(item) for item in session.scalars(
            select(RawItem).join(Source).where(RawItem.id.in_(without_run), Source.city == city),
        )
    ]
    if not unmatched:
        return []
    posts = session.execute(select(EditorialRun, RawItem).join(RawItem).join(Source).where(
        Source.city == city, EditorialRun.delivery_status == "sent",
    ).order_by(EditorialRun.raw_item_id.desc()))
    for run, raw in posts:
        previous = _candidate(raw)
        if any(same_event(candidate, previous) for candidate in unmatched):
            return [run]
    return []


def _news_state(session: Session, row: Submission, now: datetime) -> str | None:
    if row.status == "closed" and _resolution(row).get("outcome") == "owner_replied":
        # Ответ о техническом ожидании не теряет позднейшую подтверждённую публикацию.
        # Отменённые обращения и неясные квитанции этим путём не возобновляются.
        previous_final = _resolution(row).get("final", {})
        raw_ids = {value for value in row.payload.get("raw_item_ids", []) if _positive(value)}
        if previous_final.get("status") != "sent" or not raw_ids:
            return None
        for run in session.scalars(select(EditorialRun).where(
            EditorialRun.raw_item_id.in_(raw_ids), EditorialRun.delivery_status == "sent",
        )):
            previous = run.outcome.get("_previous_policy_hold", {}).get("outcome", {})
            post_id = _confirmed_post(session, run, row.payload["city"])
            if previous.get("reason") == "event_place_requires_verification" and post_id:
                _set_resolution(row, previous_final=previous_final)
                _close(row, "Материал по вашему обращению опубликован. "
                       f"https://t.me/eduvbelgrad/{post_id}", "published", now=now)
                break
        return None
    if row.status == "reader_review":
        if discovery_deadline_reached(row, now):
            _close(row, _NO_PUBLIC_CONFIRMATION, "public_confirmation_missing", now=now)
            return None
        if _primary_review_available(row):
            return ("Сигнал из чужого источника. Можно проверить первоисточник отдельной кнопкой. "
                    "Присланные текст и файлы остаются только редакции.")
        if confirmed_source_signal(row.payload):
            return ("Подтверждение ищем в плановых циклах по публичным источникам. "
                    "Текст и вложения обращения остаются в редакции.")
        return "Материал читателя. Проверьте содержание, права и факты перед публикацией."
    if row.status == "expired":
        _close(row, _EXPIRED, "expired", now=now)
        return None
    if row.status == "waiting_primary":
        receipt_at = _when(row.payload, "accepted_at") or row.created_at.replace(
            tzinfo=row.created_at.tzinfo or UTC,
        )
        incident = INCIDENT.search(str(row.payload.get("text", ""))) is not None
        if now - receipt_at >= timedelta(hours=1 if incident else 48):
            _close(row, _EXPIRED, "expired", now=now)
        elif _primary_review_available(row):
            return ("Первоисточник пока не связан. После устранения причины можно повторить "
                    "проверку этой же версии отдельной кнопкой; публикация не запускается.")
        return None
    if row.status != "source_linked":
        return None

    raw_ids = {value for value in row.payload.get("raw_item_ids", []) if _positive(value)}
    if not raw_ids:
        return None
    city = row.payload["city"]
    direct_runs = list(session.scalars(
        select(EditorialRun).join(RawItem).join(Source).where(
            EditorialRun.raw_item_id.in_(raw_ids), Source.city == city,
        ),
    ))
    news_already_sent = any(_confirmed_post(session, run, city) for run in direct_runs)
    related = [] if news_already_sent else _related_runs(session, raw_ids, direct_runs, city=city)
    runs = [*direct_runs, *related]
    _set_resolution(row, primary_processing_retryable=not runs)
    for run in runs:
        if (post_id := _confirmed_post(session, run, city)) is not None:
            text = ("Материал по вашему обращению опубликован"
                    if run in direct_runs else "Эту новость уже опубликовали")
            _close(
                row, f"{text}. https://t.me/eduvbelgrad/{post_id}", "published", now=now,
            )
            return None

    if any(run.delivery_status in {"sending", "uncertain", "sent"} for run in runs):
        return "Проверьте квитанцию публикации перед ответом автору."
    if any(run.outcome.get("status") == "human_review" for run in runs):
        return "Новость ждёт редакционной проверки."
    if not runs and _primary_review_available(row):
        return ("Первоисточник найден, редакционный разбор ещё не завершён. "
                "После устранения сбоя можно повторить разбор этой же версии.")
    if len(direct_runs) == len(raw_ids) and all(
        run.delivery_status == "cancelled" or (
            run.outcome.get("status") in {"skipped", "blocked"}
            and run.outcome.get("reason") != "event_already_reviewed"
            and not str(run.outcome.get("reason", "")).startswith("draft_repeats_recent_post:")
        ) for run in direct_runs
    ):
        _close(row, _NOT_PUBLISHED, "not_published", now=now)
        return None
    accepted = _when(row.payload, "accepted_at")
    if accepted and now - accepted >= timedelta(hours=48):
        return "Проверьте, чем закончился разбор, и ответьте автору."
    return None


def _owner_enabled(owner_user_id: int | None, owner_chat_id: int | None) -> bool:
    return _positive(owner_user_id) and _positive(owner_chat_id) and owner_user_id == owner_chat_id


def _exact_owner_card(
    row: Submission | None, chat_id: int, message_id: object,
) -> bool:
    if (
        row is None or row.payload.get("reader_intake") is not True
        or row.payload.get("city") != "belgrade" or row.payload.get("test_only")
        or not _positive(message_id)
    ):
        return False
    card = _resolution(row).get("owner_notice", {})
    return bool(
        card.get("status") == "sent" and card.get("chat_id") == chat_id
        and card.get("telegram_message_id") == message_id
    )


def _primary_review_available(row: Submission) -> bool:
    return bool(
        row.kind == "news_tip" and (
            row.status in {"reader_review", "waiting_primary"}
            or row.status == "source_linked"
            and _resolution(row).get("primary_processing_retryable") is True
        )
        and confirmed_source_signal(row.payload)
        and tip_urls(row.payload)
    )


def record_reader_resolution_callback(
    session: Session, callback: object, *, owner_user_id: int | None, owner_chat_id: int | None,
) -> OwnerUpdateResult | None:
    if not isinstance(callback, dict):
        return None
    data = callback.get("data")
    match = _OWNER_ACTION.fullmatch(data) if isinstance(data, str) else None
    if not match:
        return None
    sender = callback.get("from", {})
    message = callback.get("message", {})
    chat = message.get("chat", {}) if isinstance(message, dict) else {}
    if (
        not _owner_enabled(owner_user_id, owner_chat_id)
        or not isinstance(sender, dict) or sender.get("id") != owner_user_id
        or sender.get("is_bot") or not isinstance(message, dict)
        or not isinstance(chat, dict) or chat.get("id") != owner_chat_id
    ):
        return None
    assert owner_chat_id is not None
    row = session.get(Submission, int(match.group(2)))
    message_id = message.get("message_id")
    if not _exact_owner_card(row, owner_chat_id, message_id):
        return OwnerUpdateResult(True, False, "Карточка обращения не найдена.")
    assert row is not None
    if match.group(1) == "primary":
        if row.status == "source_linked" and session.scalar(select(EditorialRun.raw_item_id).where(
            EditorialRun.raw_item_id.in_(row.payload.get("raw_item_ids", [])),
        ).limit(1)) is not None:
            return OwnerUpdateResult(True, False, "Материал уже рассмотрен; проверьте его решение.")
        if not _primary_review_available(row):
            return OwnerUpdateResult(True, False, "Нет подтверждённого сигнала с исходной ссылкой.")
        row.payload = {**row.payload, "public_source_review": {
            "retry_budget_version": 1,
            "owner_user_id": owner_user_id, "at": datetime.now(UTC).isoformat(),
            "urls": tip_urls(row.payload),
            "material_sha256": material_fingerprint(row.payload),
        }, "updated_at": datetime.now(UTC).isoformat()}
        row.status = "new"
        return OwnerUpdateResult(
            True, True, "Первоисточник отправлен на проверку. Текст и вложения обращения "
            "остаются закрытыми; публикация ещё не разрешена.",
        )
    if match.group(1) == "reject_media":
        if row.status == "closed":
            return OwnerUpdateResult(True, False, "Обращение уже закрыто.")
        row.payload = {**row.payload, "reader_media_review": {
            "decision": "rejected", "owner_user_id": owner_user_id,
            "material_sha256": material_fingerprint(row.payload),
            "at": datetime.now(UTC).isoformat(),
        }, "updated_at": datetime.now(UTC).isoformat()}
        return OwnerUpdateResult(True, True, "Вложения отклонены. Публикация не запускалась.")
    if match.group(1) == "approve_media":
        if row.status == "closed" or not row.payload.get("attachments"):
            return OwnerUpdateResult(True, False, "Нет открытых вложений для проверки.")
        # A bot/model cannot approve media; the exact private owner card is required.
        delivered = _resolution(row)
        for attachment in row.payload["attachments"]:
            file_id = attachment.get("file_id", "")
            key = f"attachment:{sha256(file_id.encode()).hexdigest()}"
            if delivered.get(key, {}).get("status") != "sent":
                return OwnerUpdateResult(
                    True, False, "Сначала получите все файлы кнопкой «Вложения» и просмотрите их.",
                )
        row.payload = {**row.payload, "reader_media_review": {
            "decision": "approved", "owner_user_id": owner_user_id,
            "material_sha256": material_fingerprint(row.payload),
            "at": datetime.now(UTC).isoformat(),
        }, "updated_at": datetime.now(UTC).isoformat()}
        return OwnerUpdateResult(
            True, True, "Ручная проверка содержания сохранена. Публикация не запускалась.",
        )
    if match.group(1) == "media":
        if not _pending_media(row):
            return OwnerUpdateResult(True, False, "Новых вложений нет.")
        _set_resolution(row, media_requested=True)
        return OwnerUpdateResult(True, True, "Вложения подготовлены к отправке в этот чат.")
    if row.status == "closed":
        return OwnerUpdateResult(
            True, False, f"Обращение №{submission_reference(row)} уже закрыто.",
        )
    _close(row, _NO_ACTION, "owner_closed", now=datetime.now(UTC))
    return OwnerUpdateResult(True, True, "Решение сохранено. Автору отправим ответ.",
                             owner_chat_id, message_id)


def record_reader_resolution_reply(
    session: Session, message: dict[str, Any], *,
    owner_user_id: int | None, owner_chat_id: int | None,
) -> OwnerUpdateResult | None:
    sender = message.get("from", {})
    chat = message.get("chat", {})
    reply = message.get("reply_to_message", {})
    if (
        not _owner_enabled(owner_user_id, owner_chat_id)
        or not isinstance(sender, dict) or sender.get("id") != owner_user_id
        or sender.get("is_bot") or not isinstance(chat, dict) or chat.get("id") != owner_chat_id
        or not isinstance(reply, dict) or not _positive(reply.get("message_id"))
    ):
        return None
    assert owner_chat_id is not None
    reply_id = reply["message_id"]
    card = Submission.payload["reader_resolution"]["owner_notice"]
    row = session.scalar(select(Submission).where(
        Submission.payload["reader_intake"].as_boolean().is_(True),
        cast(card["chat_id"].as_string(), BigInteger) == owner_chat_id,
        cast(card["telegram_message_id"].as_string(), BigInteger) == reply_id,
    ))
    if not _exact_owner_card(row, owner_chat_id, reply_id):
        return None
    assert row is not None
    if row.status == "closed":
        return OwnerUpdateResult(
            True, False, f"Обращение №{submission_reference(row)} уже закрыто.",
        )
    text = message.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 1800:
        return OwnerUpdateResult(True, False, "Пришлите итоговый ответ текстом до 1800 знаков.")
    _close(row, text.strip(), "owner_replied", now=datetime.now(UTC))
    return OwnerUpdateResult(True, True, "Ответ сохранён. Автору отправим его в личный чат.",
                             owner_chat_id, reply_id)


async def _send_receipted(
    session: Session, row: Submission, bot: TelegramBotAPI, key: str, params: dict[str, Any],
    *, method: str = "sendMessage",
) -> str:
    record = dict(_resolution(row).get(key, {}))
    attempted = datetime.now(UTC)
    record.update(status="sending", attempted_at=attempted.isoformat(), chat_id=params["chat_id"])
    _set_resolution(row, **{key: record})
    session.commit()
    try:
        reply = await bot._call(method, params)
        if not isinstance(reply, dict) or not _positive(reply.get("message_id")):
            raise ValueError("invalid Telegram reader response receipt")
    except Exception as error:
        record.update(status="uncertain", error_type=type(error).__name__)
    else:
        now = datetime.now(UTC)
        record.update(
            status="sent", telegram_message_id=reply["message_id"], sent_at=now.isoformat(),
            user_wait_seconds=_elapsed(row.payload, "received_at", now),
            accepted_wait_seconds=_elapsed(row.payload, "accepted_at", now),
        )
    _set_resolution(row, **{key: record})
    session.commit()
    return str(record["status"])


def _owner_card(row: Submission, reason: str, owner_chat_id: int) -> dict[str, Any]:
    text = str(row.payload.get("text", "")).strip()
    excerpt = text[:1300] + ("…" if len(text) > 1300 else "")
    instructions = (
        "Проверьте исходный пост. Если исправили публикацию или карту, "
        "укажите в ответе, что изменилось."
        if row.kind == "correction" else reason
    )
    publication_details = owner_publication_summary(row.payload)
    if row.payload.get("reader_publication"):
        publication_details += "Ручной контроль: " + (
            human_review_reason(row.payload, owner_chat_id) or "пройден для этой версии."
        ) + "\n"
    return {
        "chat_id": owner_chat_id,
        "text": (
            f"Предложка №{submission_reference(row)}. {_OWNER_KINDS.get(row.kind, 'Новость')}\n\n"
            f"{excerpt or 'Без текста.'}\n\n"
            f"Вложений: {len(row.payload.get('attachments', []))}.\n"
            f"{publication_details}"
            f"{instructions}\n\n"
            "Ответьте на эту карточку готовым текстом. Он уйдёт автору обращения."
        ),
        "reply_markup": {"inline_keyboard": [
            *([[{"text": ("Проверить первоисточник" if row.status == "reader_review"
                          else "Повторить проверку источника"),
                 "callback_data": f"rr:primary:{row.id}"}]]
              if _primary_review_available(row) else []),
            [{"text": "Вложения", "callback_data": f"rr:media:{row.id}"}],
            *([[{"text": "Содержание вложений проверено",
                 "callback_data": f"rr:approve_media:{row.id}"}],
               [{"text": "Отклонить вложения", "callback_data": f"rr:reject_media:{row.id}"}]]
              if row.payload.get("attachments") and row.payload.get("reader_publication") else []),
            [{"text": "Закрыть без действий", "callback_data": f"rr:close:{row.id}"}],
        ]},
        "protect_content": True,
        "allow_paid_broadcast": False,
        "link_preview_options": {"is_disabled": True},
    }


def _owner_card_hash(params: dict[str, Any]) -> str:
    return sha256(str(params["text"]).encode()).hexdigest()


async def _refresh_owner_card(
    session: Session, row: Submission, bot: TelegramBotAPI, reason: str, owner_chat_id: int,
) -> str | None:
    state = _resolution(row)
    card = state.get("owner_notice", {})
    if (card.get("status") != "sent" or card.get("chat_id") != owner_chat_id
            or not _positive(card.get("telegram_message_id"))):
        return None
    if not card.get("content_sha256") and not state.get("owner_update"):
        updated, sent = _when(row.payload, "updated_at"), _when(card, "sent_at")
        if updated is None or sent is None or updated <= sent:
            return None
    params = _owner_card(row, reason, owner_chat_id)
    digest = _owner_card_hash(params)
    current = state.get("owner_update", {})
    if current.get("content_sha256", card.get("content_sha256")) == digest:
        _set_resolution(row, owner_update={**current, "content_sha256": digest,
                        "source_updated_at": row.payload.get("updated_at")})
        session.commit()
        return "unchanged"
    # Editing the same card keeps reply routing and media buttons intact.
    # An unknown edit is not retried for the same content; a newer version may replace it.
    _set_resolution(row, owner_update={"content_sha256": digest,
                    "source_updated_at": row.payload.get("updated_at")})
    return await _send_receipted(session, row, bot, "owner_update", {
        **params, "message_id": card["telegram_message_id"],
    }, method="editMessageText")


def _pending_media(row: Submission) -> list[tuple[str, str, str]]:
    resolution = _resolution(row)
    result = []
    for attachment in row.payload.get("attachments", []):
        file_id = attachment.get("file_id")
        kind = attachment.get("kind")
        if kind in _MEDIA_METHODS and isinstance(file_id, str) and file_id:
            key = f"attachment:{sha256(file_id.encode()).hexdigest()}"
            if key not in resolution:
                result.append((key, kind, file_id))
    return result


async def _owner_media(
    session: Session, row: Submission, bot: TelegramBotAPI, *, owner_chat_id: int,
) -> tuple[int, int]:
    card = _resolution(row).get("owner_notice", {})
    if card.get("chat_id") != owner_chat_id or card.get("status") != "sent":
        return 0, 0
    sent = 0
    uncertain = 0
    pending = _pending_media(row)
    for key, kind, file_id in pending[:3]:
        result = await _send_receipted(session, row, bot, key, {
            "chat_id": owner_chat_id, kind: file_id,
            "caption": f"Вложение к заявке №{submission_reference(row)}",
            "protect_content": True, "allow_paid_broadcast": False,
            "reply_parameters": {"message_id": card["telegram_message_id"]},
        }, method=_MEDIA_METHODS[kind])
        sent += int(result == "sent")
        uncertain += int(result == "uncertain")
    _set_resolution(row, media_requested=len(pending) > 3,
                    media_uncertain=(_resolution(row).get("media_uncertain", 0) + uncertain))
    session.commit()
    return sent, uncertain


async def _clear_closed_reader_keyboard(
    session: Session, row: Submission, bot: TelegramBotAPI, *, owner_chat_id: int,
) -> bool:
    state = _resolution(row)
    card, receipt = state.get("owner_notice", {}), state.get("owner_keyboard", {})
    now = datetime.now(UTC)
    retry = _when(receipt, "retry_at")
    if (row.status != "closed" or not row.payload.get("reader_publication")
            or card.get("status") != "sent" or card.get("chat_id") != owner_chat_id
            or not _positive(card.get("telegram_message_id"))
            or receipt.get("status") == "cleared" or (retry is not None and retry > now)):
        return False
    record = {"status": "clearing", "attempted_at": now.isoformat()}
    _set_resolution(row, owner_keyboard=record)
    session.commit()
    try:
        result = await bot._call("editMessageReplyMarkup", {
            "chat_id": owner_chat_id, "message_id": card["telegram_message_id"],
            "reply_markup": {"inline_keyboard": []},
        })
        if not (result is True or isinstance(result, dict)
                and result.get("message_id") == card["telegram_message_id"]):
            raise ValueError("invalid keyboard edit receipt")
    except Exception as error:
        # Empty markup is an idempotent projection, not a new message.
        # An interrupted edit may be repeated; final reader answers never are.
        if isinstance(error, TelegramAPIError) and "message is not modified" in str(error).lower():
            record["status"] = "cleared"
        else:
            record.update(status="retry", error_type=type(error).__name__,
                          retry_at=(now + timedelta(minutes=2)).isoformat())
    else:
        record["status"] = "cleared"
    _set_resolution(row, owner_keyboard=record)
    session.commit()
    return True


async def process_reader_resolutions(
    engine: Engine, bot: TelegramBotAPI, *, enabled: bool,
    owner_user_id: int | None = None, owner_chat_id: int | None = None, limit: int = 20,
) -> dict[str, Any]:
    stats: dict[str, Any] = dict(
        changed=0, closed=0, sent=0, owner_sent=0, media_sent=0, uncertain=0,
        blocked=0, unresolved=0, destination_unavailable=0, response_seconds=[],
    )
    if not enabled:
        return {"status": "disabled", **stats}
    query = select(Submission.id).where(
        Submission.payload["reader_intake"].as_boolean().is_(True),
        Submission.payload["city"].as_string() == "belgrade",
        Submission.payload["test_only"].as_boolean().is_not(True),
        or_(
            and_(Submission.kind == "news_tip", Submission.status == "closed",
                 Submission.payload["reader_resolution"]["outcome"].as_string()
                 == "owner_replied",
                 Submission.payload["raw_item_ids"].as_string().is_not(None)),
            and_(Submission.status == "closed",
                 Submission.payload["reader_resolution"]["final"]["status"].as_string()
                 == "pending"),
            and_(Submission.status == "closed",
                 Submission.payload["reader_publication"]["version"].as_string().is_not(None),
                 Submission.payload["reader_resolution"]["owner_notice"]["status"].as_string()
                 == "sent",
                 func.coalesce(
                     Submission.payload["reader_resolution"]["owner_keyboard"]["status"].as_string(),
                     "",
                 ) != "cleared"),
            and_(Submission.kind == "news_tip",
                 Submission.status.in_(("expired", "waiting_primary", "source_linked"))),
            and_(Submission.kind == "news_tip", Submission.status == "reader_review",
                 Submission.payload["reader_resolution"]["owner_notice"]["status"]
                 .as_string().is_(None)),
            and_(Submission.kind == "news_tip", Submission.status == "reader_review",
                 Submission.payload["reader_publication"]["ownership"].as_string() == "source",
                 func.coalesce(Submission.payload["reader_resolution"]
                               ["primary_review_offered"].as_boolean(), False).is_(False)),
            and_(Submission.kind == "news_tip", Submission.status == "reader_review",
                 Submission.payload[DISCOVERY_FIELD]["checked_at"].as_string().is_not(None)),
            and_(Submission.kind.in_(_OWNER_KINDS), Submission.status == "new",
                 Submission.payload["reader_resolution"]["owner_notice"]["status"]
                 .as_string().is_(None)),
            Submission.payload["reader_resolution"]["media_requested"].as_boolean().is_(True),
            and_(
                Submission.status != "closed",
                Submission.payload["reader_resolution"]["owner_notice"]["status"]
                .as_string() == "sent",
                Submission.payload["updated_at"].as_string() > func.coalesce(
                    Submission.payload["reader_resolution"]["owner_update"]["source_updated_at"]
                    .as_string(),
                    Submission.payload["reader_resolution"]["owner_notice"]["sent_at"]
                    .as_string(),
                ),
            ),
        ),
    ).order_by(Submission.id)
    with Session(engine) as session:
        ids = list(session.scalars(query))
    for sid in ids:
        if stats["changed"] >= limit:
            break
        with Session(engine) as session:
            row = session.get(Submission, sid)
            assert row is not None
            reason = (
                _news_state(session, row, datetime.now(UTC)) if row.kind == "news_tip" else None
            )
            state = _resolution(row)
            if row.status == "closed" and state.get("final", {}).get("status") == "pending":
                if session.is_modified(row):
                    session.commit()
                    stats["closed"] += 1
                    stats["changed"] += 1
                try:
                    destination = await reader_destination(bot, row)
                except (httpx.HTTPError, TelegramAPIError):
                    stats["destination_unavailable"] += 1
                    continue
                if destination is None:
                    _set_resolution(row, final={**state["final"], "status": "blocked"})
                    session.commit()
                    stats["blocked"] += 1
                else:
                    params = {
                        **destination,
                        "text": (
                            f"Обращение №{submission_reference(row)}\n\n{state['final']['text']}"
                        ),
                        "protect_content": True,
                        "allow_paid_broadcast": False,
                        "link_preview_options": {"is_disabled": True},
                    }
                    outcome = await _send_receipted(session, row, bot, "final", params)
                    stats[outcome] += 1
                    delay = _resolution(row)["final"].get("accepted_wait_seconds")
                    if outcome == "sent" and delay is not None:
                        stats["response_seconds"].append(delay)
                stats["changed"] += 1
            elif (
                (reason or row.kind in _OWNER_KINDS)
                and row.status != "closed" and not state.get("owner_notice")
                and _owner_enabled(owner_user_id, owner_chat_id)
            ):
                assert owner_chat_id is not None
                params = _owner_card(row, reason or "Рассмотрите обращение.", owner_chat_id)
                _set_resolution(row, owner_notice={"content_sha256": _owner_card_hash(params)})
                outcome = await _send_receipted(session, row, bot, "owner_notice", params)
                stats["owner_sent" if outcome == "sent" else outcome] += 1
                stats["changed"] += 1
            elif row.status != "closed" and _owner_enabled(owner_user_id, owner_chat_id):
                assert owner_chat_id is not None
                refresh_outcome = await _refresh_owner_card(
                    session, row, bot, reason or "Рассмотрите обращение.", owner_chat_id,
                )
                if refresh_outcome is not None:
                    stats["uncertain"] += int(refresh_outcome == "uncertain")
                    stats["changed"] += 1
            if state.get("media_requested") and _owner_enabled(owner_user_id, owner_chat_id):
                assert owner_chat_id is not None
                sent, uncertain = await _owner_media(session, row, bot, owner_chat_id=owner_chat_id)
                stats["media_sent"] += sent
                stats["uncertain"] += uncertain
                stats["changed"] += sent + uncertain
            if _owner_enabled(owner_user_id, owner_chat_id):
                assert owner_chat_id is not None
                stats["changed"] += int(await _clear_closed_reader_keyboard(
                    session, row, bot, owner_chat_id=owner_chat_id,
                ))
                if (_primary_review_available(row)
                        and not _resolution(row).get("primary_review_offered")):
                    _set_resolution(row, primary_review_offered=True)
                    session.commit()
    with Session(engine) as session:
        resolution = Submission.payload["reader_resolution"]
        uncertain_delivery = [
            resolution[key]["status"].as_string().in_(("sending", "blocked", "uncertain"))
            for key in ("final", "owner_notice", "owner_update")
        ]
        unresolved = select(func.count()).select_from(Submission).where(
            Submission.payload["reader_intake"].as_boolean().is_(True),
            Submission.payload["city"].as_string() == "belgrade",
            or_(*uncertain_delivery, resolution["media_uncertain"].as_integer() > 0),
        )
        stats["unresolved"] = session.scalar(unresolved) or 0
    return {"status": "requires_review" if stats["unresolved"] else "ready", **stats}
