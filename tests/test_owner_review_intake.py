import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_private_intake import engine as _engine

from relocant_media.services.owner_review import OWNER_REVIEW_KIND
from relocant_media.services.private_intake import receive_private_tips
from relocant_media.storage.models import Submission, TelegramUpdateReceipt

engine = _engine
OWNER = 9001
OWNER_CHAT = 9001


class OwnerBot:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    async def _call(self, method, params):
        self.calls.append((method, params))
        if method == "getWebhookInfo":
            return {"url": ""}
        if method == "getMe":
            return {"id": 42}
        if method == "getChatMember":
            return {"can_manage_direct_messages": True}
        if method == "getUpdates":
            return self.updates
        if method == "answerCallbackQuery":
            return True
        if method == "editMessageReplyMarkup":
            return True
        raise AssertionError(method)


def _review(session: Session, *, message_id: int = 501) -> Submission:
    review = Submission(
        kind=OWNER_REVIEW_KIND,
        telegram_user_id=OWNER,
        telegram_message_id=message_id,
        status="pending",
        payload={
            "city": "belgrade",
            "raw_item_id": 777,
            "post_id": 888,
            "owner_chat_id": OWNER_CHAT,
            "package_sha256": "a" * 64,
            "draft_sha256": "b" * 64,
            "category": "politics",
            "version": 1,
        },
    )
    session.add(review)
    session.commit()
    return review


@pytest.mark.asyncio
async def test_owner_publish_callback_is_consumed_before_normal_intake(engine):
    with Session(engine) as session:
        review = _review(session)
        review_id = review.id
    update = {
        "update_id": 101,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": OWNER, "is_bot": False},
            "data": f"or:publish:{review_id}",
            "message": {"message_id": 501, "chat": {"id": OWNER_CHAT}},
        },
    }
    bot = OwnerBot([update])
    result = await receive_private_tips(
        engine,
        bot,
        enabled=True,
        accept_private_tips=False,
        owner_user_id=OWNER,
        owner_chat_id=OWNER_CHAT,
    )
    assert result["received"] == 1 and result["owner_actions"] == 1
    assert [name for name, _ in bot.calls] == [
        "getWebhookInfo", "getUpdates", "answerCallbackQuery", "editMessageReplyMarkup"
    ]
    with Session(engine) as session:
        assert session.get(Submission, review_id).status == "approved"
        receipt = session.get(TelegramUpdateReceipt, 101)
        assert receipt is not None and receipt.kind == "owner_review"
        assert session.scalar(select(Submission).where(Submission.kind == "news_tip")) is None


@pytest.mark.asyncio
async def test_owner_reply_becomes_revision_not_news_tip(engine):
    with Session(engine) as session:
        review = _review(session)
        review_id = review.id
    update = {
        "update_id": 102,
        "message": {
            "message_id": 700,
            "from": {"id": OWNER, "is_bot": False},
            "chat": {"id": OWNER_CHAT, "type": "private"},
            "text": "Убери оценочную формулировку и сократи заголовок.",
            "reply_to_message": {"message_id": 501},
        },
    }
    bot = OwnerBot([update])
    result = await receive_private_tips(
        engine,
        bot,
        enabled=True,
        accept_private_tips=True,
        owner_user_id=OWNER,
        owner_chat_id=OWNER_CHAT,
    )
    assert result["received"] == 1 and result["owner_actions"] == 1
    assert [name for name, _ in bot.calls[:4]] == [
        "getWebhookInfo", "getMe", "getChatMember", "getUpdates"
    ]
    with Session(engine) as session:
        stored = session.get(Submission, review_id)
        assert stored.status == "revision_requested"
        assert stored.payload["owner_comment"].startswith("Убери оценочную")
        assert session.scalar(select(Submission).where(Submission.kind == "news_tip")) is None


@pytest.mark.asyncio
async def test_wrong_user_cannot_operate_owner_buttons(engine):
    with Session(engine) as session:
        review = _review(session)
        review_id = review.id
    update = {
        "update_id": 103,
        "callback_query": {
            "id": "callback-2",
            "from": {"id": OWNER + 1, "is_bot": False},
            "data": f"or:reject:{review_id}",
            "message": {"message_id": 501, "chat": {"id": OWNER_CHAT}},
        },
    }
    result = await receive_private_tips(
        engine,
        OwnerBot([update]),
        enabled=True,
        accept_private_tips=False,
        owner_user_id=OWNER,
        owner_chat_id=OWNER_CHAT,
    )
    assert result["received"] == 0 and result["owner_actions"] == 0
    with Session(engine) as session:
        assert session.get(Submission, review_id).status == "pending"
        assert session.get(TelegramUpdateReceipt, 103).status == "ignored"
