from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ops.intake_batch import collect_batch
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.private_intake import receive_private_tips
from relocant_media.services.submission_feedback import reply_to_submissions, resolve_submission
from relocant_media.services.submission_queue import batch_submissions, pending_submissions
from relocant_media.storage.models import AdRequest, Base, Post, Submission

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

    def click(self, uid, *, message=None, button, reader=READER):
        parent_id, params = message or self.messages[-1]
        action = next(item["callback_data"] for group in params["reply_markup"]["inline_keyboard"]
                      for item in group if item["text"] == button)
        target = {"chat": self.chat, "message_id": parent_id}
        if self.direct:
            target["direct_messages_topic"] = {"topic_id": TOPIC}
        return {"update_id": uid, "callback_query": {"id": f"cb-{uid}",
                "from": {"id": reader}, "message": target, "data": action}}

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

    await telegram.receive(engine, telegram.click(2, button="Предложить новость"))
    assert any(method == "answerCallbackQuery" for method, _ in telegram.calls)
    prompt = telegram.messages[-1]
    assert "Что, где и когда произошло?" in prompt[1]["text"]

    if not direct:
        await telegram.receive(engine, telegram.message(
            3, caption="Дорожное объявление", photo=[{"file_id": "photo-small"}, photo],
        ))
    await telegram.receive(engine, telegram.message(4, caption="Место работ", video=video))
    await telegram.receive(engine, telegram.click(5, message=prompt, button="Отправить заявку"))
    acceptance_id, acceptance = telegram.messages[-1]
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        sid = row.id
        assert row.kind == "news_tip"
        assert row.status == "new"
        assert row.payload["text"] == "Дорожное объявление\nМесто работ"
        assert row.payload["attachments"] == [
            {"kind": "photo", **photo}, {"kind": "video", **video},
        ]
        assert [item.id for item in batch_submissions(session)] == [sid]
        assert session.scalar(select(Post)) is None
    assert f"№{sid}" in acceptance["text"]
    assert "принята" in acceptance["text"]

    reply_to = {"message_id": acceptance_id}
    extra = telegram.message(6, caption="Вид с тротуара", photo=[{"file_id": "extra-photo"}],
                             reply_to_message=reply_to)
    confirmed = await telegram.receive(engine, extra)
    assert confirmed["reader_notices"]["sent"] == 1
    assert f"№{sid}" in telegram.messages[-1][1]["text"]
    assert telegram.messages[-1][1]["reply_parameters"]["message_id"] == 6

    replay = await telegram.receive(engine, extra)
    assert replay["received"] == 0
    assert len(telegram.messages) == 4
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.id == sid
        assert row.payload["attachments"][-1]["file_id"] == "extra-photo"
        assert row.payload["text"].endswith("\nВид с тротуара")


@pytest.mark.asyncio
async def test_reader_cannot_select_another_authors_button(engine):
    telegram = Telegram(direct=True)
    await telegram.receive(engine, telegram.message(1, text="Заявка"))
    await telegram.receive(engine, telegram.click(2, button="Пожаловаться", reader=READER + 1))
    assert len(telegram.messages) == 1
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        assert row.status == "choosing"
        assert row.kind != "complaint"


@pytest.mark.asyncio
async def test_reader_stale_submit_cannot_send_changed_topic(engine):
    telegram = Telegram(direct=False)
    await telegram.receive(engine, telegram.message(1, text="Описание"))
    await telegram.receive(engine, telegram.click(2, button="Предложить новость"))
    old_prompt = telegram.messages[-1]
    await telegram.receive(engine, telegram.click(3, button="Сменить тему"))
    await telegram.receive(engine, telegram.click(4, button="Пожаловаться"))
    new_prompt = telegram.messages[-1]

    await telegram.receive(engine, telegram.click(5, message=old_prompt, button="Отправить заявку"))
    with Session(engine) as session:
        still_draft = session.scalars(select(Submission)).one()
        assert still_draft.kind == "complaint"
        assert still_draft.status == "collecting"

    await telegram.receive(engine, telegram.click(6, message=new_prompt, button="Отправить заявку"))
    with Session(engine) as session:
        accepted = session.scalars(select(Submission)).one()
        assert accepted.kind == "complaint"
        assert accepted.status == "new"


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True], ids=["bot-chat", "channel-dm"])
async def test_reader_confirmation_precedes_editorial_and_followup_reopens_case(engine, direct):
    telegram = Telegram(direct)
    start = telegram.message(1, ago=42, text="На улице появились ограждения")
    await telegram.receive(engine, start)
    await telegram.receive(engine, telegram.click(2, button="Предложить новость"))
    telegram.updates = [telegram.click(3, button="Отправить заявку")]

    def editorial():
        assert "принята" in telegram.messages[-1][1]["text"]
        return 0

    result = await collect_batch(
        engine, telegram, enabled=True, store_guard=lambda: nullcontext(True),
        run_pipeline=editorial, state={}, reader_feedback_enabled=True,
    )
    assert result["pipeline_started"] is True
    with Session(engine) as session:
        row = session.scalars(select(Submission)).one()
        sid = row.id
        resolve_submission(row, [], now=datetime.now(UTC))
        session.commit()

    feedback = await reply_to_submissions(engine, telegram, enabled=True, only_ids={sid})
    assert feedback["sent"] == 1
    clarification_id, clarification = telegram.messages[-1]
    assert str(sid) in clarification["text"]
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
    ("Сообщить об ошибке", "correction"), ("Реклама", "ad_request"),
])
async def test_reader_correction_and_ads_stay_out_of_news_batch(engine, button, kind):
    telegram = Telegram(direct=True)
    await telegram.receive(engine, telegram.message(1, text="Описание https://example.org/a"))
    await telegram.receive(engine, telegram.click(2, button=button))
    click = telegram.click(3, button="Отправить заявку")
    await telegram.receive(engine, click)
    await telegram.receive(engine, click)
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
