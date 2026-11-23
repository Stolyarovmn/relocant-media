import re
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import relocant_media.api.app as api
from relocant_media.ops.intake_batch import collect_batch
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.private_intake import receive_private_tips
from relocant_media.services.reader_forwarded_source import public_forward_urls
from relocant_media.services.reader_publication import material_fingerprint
from relocant_media.services.submission_feedback import reply_to_submissions, resolve_submission
from relocant_media.services.submission_queue import batch_submissions, pending_submissions
from relocant_media.services.submission_reference import PUBLIC_REFERENCE_PATTERN
from relocant_media.storage.models import AdRequest, Base, Post, Submission, TelegramUpdateReceipt

READER = 29
DM_CHAT = -2000000000011
TOPIC = 23


class Telegram:
    def __init__(self, direct):
        self.direct = direct
        self.updates = []
        self.calls = []
        self.messages = []
        self.parent = APPROVED_CHANNEL_ID
        self.send_timeout = False

    @property
    def chat(self):
        if self.direct:
            return {"id": DM_CHAT, "is_direct_messages": True, "type": "supergroup"}
        return {"id": READER, "type": "private"}

    def message(self, uid, *, ago=0, **fields):
        epoch = int((datetime.now(UTC) - timedelta(seconds=ago)).timestamp())
        message = {"message_id": uid, "from": {"id": READER}, "chat": self.chat,
                   "date": epoch, **fields}
        if self.direct:
            message["direct_messages_topic"] = {"topic_id": TOPIC}
        return {"update_id": uid, "message": message}

    def press(self, uid, button, *, message=None, ago=0):
        _, params = message or self.messages[-1]
        markup = params["reply_markup"]
        assert "inline_keyboard" not in markup
        assert any(item["text"] == button for row in markup["keyboard"] for item in row)
        return self.message(uid, ago=ago, text=button)

    def legacy_click(self, uid, *, sid, action, message=None, reader=READER):
        parent_id, _ = message or self.messages[-1]
        target = {"chat": self.chat, "message_id": parent_id}
        if self.direct:
            target["direct_messages_topic"] = {"topic_id": TOPIC}
        return {"update_id": uid, "callback_query": {"id": f"cb-{uid}",
                "from": {"id": reader}, "message": target, "data": f"intake:{sid}:{action}"}}

    async def _call(self, method, params):
        self.calls.append((method, params))
        if method == "sendMessage":
            if self.send_timeout:
                raise httpx.ReadTimeout("simulated Telegram timeout")
            assert params["chat_id"] == self.chat["id"]
            if self.direct:
                assert params["direct_messages_topic_id"] == TOPIC
            else:
                assert "direct_messages_topic_id" not in params
            assert params.get("protect_content") is True
            mid = len(self.messages) + 900
            self.messages.append((mid, params))
            return {"message_id": mid}
        return {
            "getWebhookInfo": {"url": ""},
            "getMe": {"id": 1},
            "getChatMember": {"can_manage_direct_messages": True},
            "getChat": {"is_direct_messages": self.direct, "parent_chat": {"id": self.parent}},
            "getUpdates": self.updates,
            "answerCallbackQuery": True,
            "editMessageReplyMarkup": True,
        }[method]

    async def receive(self, engine, *updates):
        self.updates = list(updates)
        return await receive_private_tips(engine, self, enabled=True, reader_feedback_enabled=True)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def submission_id(engine):
    with Session(engine) as session:
        return session.scalars(select(Submission.id).order_by(Submission.id.desc())).first()


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_public_forward_is_kept_as_content_from_both_reader_entries(engine, direct):
    bot = Telegram(direct)
    await bot.receive(engine, bot.message(
        30, text="Предложить новость", forward_origin={
            "type": "channel", "chat": {"type": "channel", "username": "Serbia"},
            "message_id": 197,
        },
    ))
    await bot.receive(engine, bot.message(
        31, text="Дополнение", forward_origin={
            "type": "channel", "chat": {"type": "channel"}, "message_id": 20,
        },
    ))
    with Session(engine) as session:
        row, = session.scalars(select(Submission))
        assert row.status == "choosing"
        assert row.payload["text"] == "Предложить новость\nДополнение"
        assert public_forward_urls(row.payload) == ["https://t.me/serbia/197"]
        changed = {**row.payload, "reader_parts": {**row.payload["reader_parts"], "30": {
            "text": "Предложить новость", "attachments": [],
            "public_forward_url": "https://t.me/serbia/198",
        }}}
        assert material_fingerprint(row.payload) != material_fingerprint(changed)


def public_reference(engine, sid):
    with Session(engine) as session:
        reference = session.get(Submission, sid).payload["public_reference"]
        assert re.fullmatch(PUBLIC_REFERENCE_PATTERN, reference)
        return reference


def use_legacy_menu(engine, sid):
    with Session(engine) as session:
        row = session.get(Submission, sid)
        notices = {
            key: {field: value for field, value in notice.items() if field != "submit_on_pick"}
            for key, notice in row.payload["reader_notices"].items()
        }
        row.payload = {**row.payload, "reader_notices": notices}
        session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize(('status', 'expected'), [
    ('cancelled', 'уже отменён'), ('closed', 'уже закрыто'), ('new', 'уже отправлено в работу'),
])
async def test_legacy_callback_reports_actual_terminal_status(engine, direct, status, expected):
    telegram = Telegram(direct)
    await telegram.receive(engine, telegram.message(1, text='Описание тестового обращения'))
    sid = submission_id(engine)
    menu = telegram.messages[-1]
    with Session(engine) as session:
        session.get(Submission, sid).status = status
        session.commit()
    await telegram.receive(engine, telegram.legacy_click(2, sid=sid, action='cancel', message=menu))
    answer = [params['text'] for method, params in telegram.calls
              if method == 'answerCallbackQuery'][-1]
    assert expected in answer
    with Session(engine) as session:
        assert session.get(Submission, sid).status == status
        assert len(session.scalars(select(Submission)).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
async def test_reader_news_menu_number_and_media_followup(engine, direct):
    telegram = Telegram(direct)
    photo = {"file_id": "photo-large", "file_unique_id": "image-1"}
    video = {"file_id": "video", "file_unique_id": "video-1"}
    start = telegram.message(1, ago=50, **(
        {"caption": "Дорожное объявление", "photo": [{"file_id": "photo-small"}, photo]}
        if direct else {"text": "/start"}
    ))
    first = await telegram.receive(engine, start)
    assert first["received"] == 1
    assert first["reader_notices"]["sent"] == 1
    assert first["reader_notices"]["response_seconds"]["menu"][0] >= 50
    assert telegram.messages[-1][1]["reply_parameters"]["message_id"] == 1

    sid = submission_id(engine)
    reference = public_reference(engine, sid)
    menu = telegram.messages[-1]
    assert reference not in menu[1]["text"]
    assert menu[1]["reply_markup"]["is_persistent"] is True
    assert menu[1]["reply_markup"]["one_time_keyboard"] is False
    materials = []
    if not direct:
        materials.append(telegram.message(
            2, caption="Дорожное объявление", photo=[{"file_id": "photo-small"}, photo],
        ))
    materials.append(telegram.message(3, caption="Место работ", video=video))
    click = telegram.press(4, "Предложить новость", message=menu, ago=200)
    repeated_click = telegram.press(5, "Предложить новость", message=menu, ago=200)
    submitted = await telegram.receive(engine, *materials, click, repeated_click)
    assert submitted["reader_notices"]["response_seconds"]["accepted"][0] >= 200
    assert len(telegram.messages) == 2
    acceptance_id, acceptance = telegram.messages[-1]
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.id == sid
        assert row.kind == "news_tip"
        assert row.status == "new"
        assert row.payload["text"] == "Дорожное объявление\nМесто работ"
        assert row.payload["attachments"] == [
            {"kind": "photo", **photo}, {"kind": "video", **video},
        ]
        assert [item.id for item in batch_submissions(session)] == [sid]
        assert session.scalar(select(Post)) is None
    assert acceptance["text"].startswith(
        f"Ваше обращение №{reference} отправлено в работу.",
    )
    assert not any(method == "answerCallbackQuery" for method, _ in telegram.calls)

    reply_to = {"message_id": acceptance_id}
    extra = telegram.message(6, caption="Вид с тротуара", photo=[{"file_id": "extra-photo"}],
                             reply_to_message=reply_to)
    confirmed = await telegram.receive(engine, extra)
    assert confirmed["reader_notices"]["sent"] == 1
    assert f"№{reference}" in telegram.messages[-1][1]["text"]
    assert telegram.messages[-1][1]["reply_parameters"]["message_id"] == 6

    replay = await telegram.receive(engine, extra)
    assert replay["received"] == 0
    assert len(telegram.messages) == 3
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.id == sid
        assert row.payload["attachments"][-1]["file_id"] == "extra-photo"
        assert row.payload["text"].endswith("\nВид с тротуара")


@pytest.mark.asyncio
async def test_reader_cannot_select_another_authors_button(engine):
    telegram = Telegram(direct=True)
    await telegram.receive(engine, telegram.message(1, text="Заявка"))
    sid = submission_id(engine)
    await telegram.receive(
        engine, telegram.legacy_click(2, sid=sid, action="pick:complaint", reader=READER + 1),
    )
    assert len(telegram.messages) == 1
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.status == "choosing"
        assert row.kind != "complaint"


@pytest.mark.asyncio
async def test_reader_stale_submit_cannot_send_changed_topic(engine):
    telegram = Telegram(direct=False)
    await telegram.receive(engine, telegram.message(1, text="Описание"))
    sid = submission_id(engine)
    use_legacy_menu(engine, sid)
    await telegram.receive(engine, telegram.legacy_click(2, sid=sid, action="pick:news_tip"))
    old_prompt = telegram.messages[-1]
    await telegram.receive(engine, telegram.legacy_click(3, sid=sid, action="menu"))
    use_legacy_menu(engine, sid)
    await telegram.receive(engine, telegram.legacy_click(4, sid=sid, action="pick:complaint"))
    new_prompt = telegram.messages[-1]

    await telegram.receive(
        engine, telegram.legacy_click(5, sid=sid, action="submit", message=old_prompt),
    )
    with Session(engine) as session:
        still_draft = session.scalars(select(Submission)).one()
        assert still_draft.kind == "complaint"
        assert still_draft.status == "collecting"

    await telegram.receive(
        engine, telegram.legacy_click(6, sid=sid, action="submit", message=new_prompt),
    )
    assert any(method == "answerCallbackQuery" for method, _ in telegram.calls)
    with Session(engine) as session:
        accepted = session.scalars(select(Submission)).one()
        assert accepted.kind == "complaint"
        assert accepted.status == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
async def test_reader_old_numbered_button_cannot_submit_another_draft(engine, direct):
    telegram = Telegram(direct)
    await telegram.receive(engine, telegram.message(1, text="Первый сюжет"))
    first_id = submission_id(engine)
    first_reference = public_reference(engine, first_id)
    use_legacy_menu(engine, first_id)
    await telegram.receive(engine, telegram.press(2, "Предложить новость"))
    first_prompt = telegram.messages[-1]
    first_submit = telegram.press(3, f"Отправить №{first_reference}", message=first_prompt)
    await telegram.receive(engine, first_submit)
    first_acceptance = telegram.messages[-1]

    await telegram.receive(engine, first_submit)
    await telegram.receive(
        engine, telegram.press(4, "Новое обращение", message=first_acceptance),
    )
    second_id = submission_id(engine)
    assert second_id != first_id
    second_reference = public_reference(engine, second_id)
    assert second_reference != first_reference
    use_legacy_menu(engine, second_id)
    await telegram.receive(engine, telegram.press(5, "Предложить новость"))
    second_prompt = telegram.messages[-1]
    await telegram.receive(engine, telegram.message(6, text="Второй сюжет"))
    await telegram.receive(
        engine, telegram.press(7, f"Отправить №{first_reference}", message=first_prompt),
    )
    with Session(engine) as session:
        first, second = session.scalars(select(Submission).order_by(Submission.id)).all()
        assert first.status == "new"
        assert first.payload["text"] == "Первый сюжет"
        assert second.status == "collecting"
        assert second.payload["text"] == "Второй сюжет"
    assert sum(f"Ваше обращение №{first_reference} отправлено в работу." in value["text"]
               for _, value in telegram.messages) == 1

    await telegram.receive(
        engine, telegram.press(8, f"Отправить №{second_reference}", message=second_prompt),
    )
    with Session(engine) as session:
        second = session.get(Submission, second_id)
        assert second.status == "new"
        assert second.payload["text"] == "Второй сюжет"


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
async def test_reader_confirmation_precedes_editorial_and_followup_reopens_case(engine, direct):
    telegram = Telegram(direct)
    start = telegram.message(1, ago=42, text="На улице появились ограждения")
    await telegram.receive(engine, start)
    sid = submission_id(engine)
    reference = public_reference(engine, sid)
    telegram.updates = [telegram.press(2, "Предложить новость")]

    def editorial():
        confirmation = telegram.messages[-1][1]["text"]
        assert f"Ваше обращение №{reference} отправлено в работу." in confirmation
        return 0

    result = await collect_batch(
        engine, telegram, enabled=True, store_guard=lambda: nullcontext(True),
        run_pipeline=editorial, state={}, reader_feedback_enabled=True,
    )
    assert result["pipeline_started"] is True
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.id == sid
        resolve_submission(row, [], now=datetime.now(UTC))
        session.commit()

    feedback = await reply_to_submissions(engine, telegram, enabled=True, only_ids={sid})
    assert feedback["sent"] == 1
    clarification_id, clarification = telegram.messages[-1]
    assert f"Обращение №{reference}" in clarification["text"]
    assert clarification["reply_parameters"]["message_id"] == 1
    await telegram.receive(engine, telegram.message(
        4, caption="Из объявления на ограждениях", video={"file_id": "video"},
        reply_to_message={"message_id": clarification_id},
    ))
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.id == sid
        assert row.status == "new"
        assert row.payload["attachments"][0]["kind"] == "video"
        assert row.payload["feedback"]["missing_primary"]["user_wait_seconds"] >= 42
        assert row.payload["feedback"]["missing_primary"]["accepted_wait_seconds"] >= 0


@pytest.mark.asyncio
async def test_reader_can_submit_a_draft_with_an_already_issued_numeric_number(engine):
    telegram = Telegram(direct=False)
    await telegram.receive(engine, telegram.message(1, text="Описание"))
    sid = submission_id(engine)
    with Session(engine) as session:
        row = session.get(Submission, sid)
        row.payload = {k: v for k, v in row.payload.items() if k != "public_reference"}
        session.commit()
    use_legacy_menu(engine, sid)
    await telegram.receive(engine, telegram.press(2, "Предложить новость"))
    await telegram.receive(engine, telegram.press(3, f"Отправить №{sid}"))
    assert f"Ваше обращение №{sid} отправлено в работу." in telegram.messages[-1][1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("direct", "opening"), [
    (False, "/start"),
    (False, "Привет!"),
    (True, "Добрый день 👋"),
])
async def test_reader_greeting_opens_menu_without_submitting(engine, direct, opening):
    telegram = Telegram(direct)
    await telegram.receive(engine, telegram.message(1, text=opening))
    sid = submission_id(engine)
    reference = public_reference(engine, sid)
    menu = telegram.messages[-1]
    assert "Можно отправить несколько сообщений и приложить фото или видео." in menu[1]["text"]
    assert reference not in menu[1]["text"]
    await telegram.receive(engine, telegram.press(2, "Предложить новость", message=menu))
    assert "Что, где и когда произошло?" in telegram.messages[-1][1]["text"]
    with Session(engine) as session:
        draft = session.get(Submission, sid)
        assert draft.status == "choosing" and draft.payload["text"] == ""
        assert not batch_submissions(session)
    intro = telegram.message(3, text="Привет! Поменялся маршрут 18.")
    second = telegram.message(4, text="Остановка на улице Негошева.")
    corrected = telegram.message(3, text="Привет! Поменялся маршрут 19.")
    edit = {"update_id": 5, "edited_message": corrected["message"]}
    click = telegram.press(6, "Предложить новость", message=menu)
    await telegram.receive(engine, intro, second, edit, click)
    with Session(engine) as session:
        row = session.get(Submission, sid)
        assert row.status == "new"
        assert row.payload["text"] == "Привет! Поменялся маршрут 19.\nОстановка на улице Негошева."
    assert f"Ваше обращение №{reference} отправлено в работу." in telegram.messages[-1][1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
async def test_waiting_reader_receives_one_instruction_and_no_presence_text(engine, direct):
    telegram = Telegram(direct)
    await telegram.receive(engine, *[
        telegram.message(uid, text=text, ago=300)
        for uid, text in enumerate(("Привет!", "Новое обращение", "есть кто?"), 1)
    ])
    assert len(telegram.messages) == 1
    assert "Весь текст и вложения отправятся одним обращением." in telegram.messages[0][1]["text"]
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.status == "choosing" and row.payload["text"] == ""
        assert not row.payload.get("reader_parts")
        assert not batch_submissions(session)
        assert len(session.scalars(select(TelegramUpdateReceipt)).all()) == 3
    # A separately polled message written before the first response is not a new request.
    await telegram.receive(engine, telegram.message(4, text="Новое обращение", ago=200))
    assert len(telegram.messages) == 1


@pytest.mark.asyncio
async def test_reader_can_reopen_menu_after_receiving_the_previous_one(engine, monkeypatch):
    import relocant_media.services.reader_intake as intake

    clock = [datetime.now(UTC)]
    monkeypatch.setattr(intake, "_now", lambda: clock[0])
    telegram = Telegram(False)
    await telegram.receive(engine, telegram.message(1, text="Описание события"))
    sid = submission_id(engine)
    clock[0] += timedelta(seconds=3)
    again = telegram.message(2, text="Новое обращение")
    again["message"]["date"] = int(clock[0].timestamp())
    await telegram.receive(engine, again)
    assert len(telegram.messages) == 2
    with Session(engine) as session:
        assert session.scalars(select(Submission.id)).all() == [sid]
        assert session.get(Submission, sid).payload["text"] == "Описание события"


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
async def test_saved_keyboard_choice_before_welcome_is_delivered_is_not_lost(engine, direct):
    telegram = Telegram(direct)
    updates = [
        telegram.message(1, text="Привет!"),
        telegram.message(2, text="Изменился маршрут автобуса на улице Негошева."),
        telegram.message(3, photo=[{"file_id": "photo", "file_unique_id": "photo-1"}]),
        telegram.message(4, text="Предложить новость"),
        telegram.message(5, text="Предложить новость"),
    ]
    await telegram.receive(engine, *updates)
    assert len(telegram.messages) == 1
    await telegram.receive(engine, telegram.message(6, text="Предложить новость", ago=300))
    assert len(telegram.messages) == 1
    with Session(engine) as session:
        assert len(session.scalars(select(Submission)).all()) == 1
    assert "отправлено в работу" in telegram.messages[0][1]["text"]
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.status == "new" and row.kind == "news_tip"
        assert row.payload["text"] == "Изменился маршрут автобуса на улице Негошева."
        assert len(row.payload["attachments"]) == 1
        assert row.payload["reader_notices"]["menu"]["status"] == "superseded"
        assert row.payload["reader_notices"]["accepted"]["status"] == "sent"
    await telegram.receive(engine, *updates)
    assert len(telegram.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
@pytest.mark.parametrize("selected", [False, True], ids=["menu", "form"])
async def test_reader_cancel_replaces_old_keyboard_with_new_entry(engine, direct, selected):
    telegram = Telegram(direct)
    await telegram.receive(engine, telegram.message(1, text="Описание"))
    sid = submission_id(engine)
    reference = public_reference(engine, sid)
    if selected:
        use_legacy_menu(engine, sid)
        await telegram.receive(engine, telegram.press(2, "Предложить новость"))
    cancellation = telegram.press(3, f"Отменить №{reference}")
    await telegram.receive(engine, cancellation)
    receipt = telegram.messages[-1][1]
    assert receipt["reply_markup"]["keyboard"] == [[{"text": "Новое обращение"}]]
    assert receipt["reply_markup"]["is_persistent"] is True
    assert f"Черновик №{reference} отменён." in receipt["text"]
    assert receipt["reply_parameters"]["message_id"] == 3
    with Session(engine) as session:
        assert session.scalars(select(Submission)).one().status == "cancelled"
        assert not batch_submissions(session)
    await telegram.receive(engine, cancellation)
    assert sum(
        msg.get("reply_markup", {}).get("keyboard") == [[{"text": "Новое обращение"}]]
        for _, msg in telegram.messages
    ) == 1
    await telegram.receive(engine, telegram.press(4, "Новое обращение"))
    assert "выберите действие ниже" in telegram.messages[-1][1]["text"]


@pytest.mark.asyncio
async def test_reader_can_clear_the_old_keyboard_of_a_cancelled_numeric_draft(engine):
    telegram = Telegram(direct=False)
    with Session(engine) as session:
        row = Submission(
            kind="reader_unclassified", status="cancelled", telegram_user_id=READER,
            payload={"reader_intake": True, "chat_id": READER,
                     "reader_notices": {"cancelled": {"status": "sent"}}},
        )
        session.add(row)
        session.flush()
        sid = row.id
        session.commit()
    await telegram.receive(engine, telegram.message(1, text=f"Отменить №{sid}"))
    receipt = telegram.messages[-1][1]
    assert receipt["reply_markup"]["keyboard"] == [[{"text": "Новое обращение"}]]
    assert f"Черновик №{sid} уже отменён." in receipt["text"]
    await telegram.receive(engine, telegram.message(2, text="Новое обращение"))
    await telegram.receive(engine, telegram.message(3, text=f"Отменить №{sid}"))
    assert "keyboard" in telegram.messages[-1][1]["reply_markup"]
    assert len(telegram.messages) == 2


@pytest.mark.asyncio
async def test_reader_notice_timeout_does_not_resend_ambiguous_delivery(engine):
    telegram = Telegram(direct=False)
    telegram.send_timeout = True
    start = telegram.message(1, text="/start")
    first = await telegram.receive(engine, start)
    assert first["reader_notices"]["uncertain"] == 1
    assert first["reader_notices"]["sent"] == 0
    telegram.send_timeout = False
    await telegram.receive(engine, start)
    await telegram.receive(engine)
    assert sum(method == "sendMessage" for method, _ in telegram.calls) == 1
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.payload["reader_notices"]["menu"]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_reader_native_request_from_other_channel_does_not_create_or_send(engine):
    telegram = Telegram(direct=True)
    telegram.parent = -1001234567890
    result = await telegram.receive(engine, telegram.message(1, text="Не наш канал"))
    assert result["received"] == 0
    assert telegram.messages == []
    with Session(engine) as session:
        assert session.scalar(select(Submission)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("button", "kind"), [
    ("Сообщить об ошибке", "correction"),
    ("Платное размещение", "ad_request"),
    ("Редакционное партнёрство", "partnership"),
    ("Реклама", "ad_request"),
    ("Сотрудничество", "partnership"),
])
async def test_reader_manual_categories_stay_out_of_news_batch(engine, button, kind):
    telegram = Telegram(direct=True)
    await telegram.receive(engine, telegram.message(1, text="Описание https://example.org/a"))
    sid = submission_id(engine)
    reference = public_reference(engine, sid)
    selected = (
        telegram.message(2, text=button) if button in {"Реклама", "Сотрудничество"}
        else telegram.press(2, button)
    )
    await telegram.receive(engine, selected)
    assert f"Ваше обращение №{reference} отправлено в работу." in telegram.messages[-1][1]["text"]
    await telegram.receive(engine, selected)
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.kind == kind
        assert row.status == "new"
        assert batch_submissions(session) == []
        assert pending_submissions(session) == []
        ads = session.scalars(select(AdRequest)).all()
        if kind == "ad_request":
            assert len(ads) == 1
            assert ads[0].payload["submission_id"] == row.id
        else:
            assert ads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
async def test_webhook_sends_menu_and_durable_ack_in_the_request(engine, direct, monkeypatch):
    telegram = Telegram(direct)
    telegram.parent = -1000000000123
    original_call = telegram._call

    async def observe_send(method, params):
        if method == "sendMessage" and params["text"].startswith("Ваше обращение №"):
            with Session(engine) as session:
                row = session.scalars(select(Submission)).one()
                assert row.status == "new"
                assert row.payload["text"] == "Место работ\nУточняющее фото"
                assert row.payload["attachments"][0]["file_id"] == "reader-photo"
                assert session.get(TelegramUpdateReceipt, 4).status == "processed"
                assert session.scalar(select(Post)) is None
        return await original_call(method, params)

    monkeypatch.setattr(telegram, "_call", observe_send)
    monkeypatch.setattr(api, "engine", engine)
    monkeypatch.setattr(api, "TelegramBotAPI", lambda *_: telegram)
    monkeypatch.setattr(api.settings, "app_env", "staging")
    monkeypatch.setattr(api.settings, "telegram_runtime_enabled", True)
    monkeypatch.setattr(api.settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(api.settings, "telegram_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(api.settings, "telegram_staging_channel_id", str(telegram.parent))

    path = "/telegram/intake-webhook"
    first = telegram.message(1, text="Здравствуйте!")
    secret = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.app), base_url="https://staging.test",
    ) as client:
        denied = await client.post(path, json=first)
        assert denied.status_code == 401 and telegram.messages == []
        opened = await client.post(path, json=first, headers=secret)
        assert opened.status_code == 200
        assert len(telegram.messages) == 1
        assert telegram.messages[0][1]["text"].startswith("Напишите, о чем хотите сообщить.")

        details = telegram.message(2, text="Место работ")
        photo = telegram.message(3, caption="Уточняющее фото", photo=[{"file_id": "reader-photo"}])
        click = telegram.press(4, "Предложить новость")
        for update in (details, photo, click, click):
            response = await client.post(path, json=update, headers=secret)
            assert response.status_code == 200

        monkeypatch.setattr(api.settings, "app_env", "prod")
        disabled = await client.post(path, json=telegram.message(5, text="/start"), headers=secret)
        assert disabled.status_code == 404

    assert len(telegram.messages) == 2
    reference = public_reference(engine, submission_id(engine))
    assert f"Ваше обращение №{reference} отправлено в работу." in telegram.messages[1][1]["text"]
    assert not any(method == "getUpdates" for method, _ in telegram.calls)
