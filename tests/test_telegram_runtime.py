import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from relocant_media.bot.runtime import TelegramRuntime
from relocant_media.storage.models import (
    AdRequest,
    ModerationAction,
    Submission,
    TelegramConversation,
    TelegramUpdateReceipt,
)

RESET = text(
    "TRUNCATE telegram_conversation, telegram_update_receipt, moderation_action, "
    "ad_request, submission RESTART IDENTITY CASCADE"
)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int | str, str]] = []
        self.deleted: list[tuple[int | str, int]] = []

    async def send_message(self, chat_id: int | str, text: str) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)

    async def delete_message(self, chat_id: int | str, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def restrict_member(self, chat_id: int | str, user_id: int) -> None:
        del chat_id, user_id


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL")
    if value is None:
        pytest.skip("TEST_DATABASE_URL is not configured")
    return value


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine(_database_url(), pool_pre_ping=True)
    with Session(engine) as session:
        session.execute(RESET)
        session.commit()
        yield session
        session.rollback()
        session.execute(RESET)
        session.commit()
    engine.dispose()


def _private_message(update_id: int, message_id: int, text_value: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": 501},
            "chat": {"id": 501, "type": "private"},
            "text": text_value,
        },
    }


def _group_message(update_id: int, message_id: int, text_value: str) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": 700},
            "chat": {"id": -100777, "type": "supergroup"},
            "text": text_value,
        },
    }


@pytest.mark.asyncio
async def test_private_intake_state_is_persistent_and_update_is_idempotent(
    db_session: Session,
) -> None:
    bot = FakeBot()
    runtime = TelegramRuntime(db_session, bot, city="belgrade")

    started = await runtime.process(_private_message(100, 10, "/tip"))
    submitted = await runtime.process(_private_message(101, 11, "Новость от читателя"))
    duplicate = await runtime.process(_private_message(101, 11, "Новость от читателя"))

    assert started.action == "awaiting_text"
    assert submitted.action == "submitted"
    assert duplicate.status == "duplicate"
    assert db_session.scalar(select(func.count()).select_from(Submission)) == 1
    assert db_session.scalar(select(func.count()).select_from(TelegramConversation)) == 0
    assert db_session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 2
    assert len(bot.sent) == 2


@pytest.mark.asyncio
async def test_ad_command_creates_submission_and_ad_request(db_session: Session) -> None:
    bot = FakeBot()
    runtime = TelegramRuntime(db_session, bot, city="belgrade")

    result = await runtime.process(_private_message(200, 20, "/ad Реклама сервиса"))

    assert result.action == "submitted"
    assert db_session.scalar(select(func.count()).select_from(Submission)) == 1
    assert db_session.scalar(select(func.count()).select_from(AdRequest)) == 1


@pytest.mark.asyncio
async def test_staging_discussion_spam_is_deleted_and_audited(db_session: Session) -> None:
    bot = FakeBot()
    runtime = TelegramRuntime(
        db_session,
        bot,
        city="belgrade",
        staging_discussion_group_id="-100777",
    )

    result = await runtime.process(
        _group_message(300, 31, "100% доход без риска, пишите в лс заработок")
    )

    assert result.action == "moderation_delete"
    assert bot.deleted == [(-100777, 31)]
    audit = db_session.scalar(select(ModerationAction))
    assert audit is not None
    assert audit.city == "belgrade"
    assert audit.action == "delete"
    assert audit.reason is not None
    assert audit.reason.startswith("rules-v2:")
    assert "percent_income_claim" in audit.reason


@pytest.mark.asyncio
async def test_ambiguous_group_promo_is_reviewed_without_delete(db_session: Session) -> None:
    bot = FakeBot()
    runtime = TelegramRuntime(
        db_session,
        bot,
        city="belgrade",
        staging_discussion_group_id="-100777",
    )

    result = await runtime.process(_group_message(301, 32, "Есть подработка, пишите мне в лс"))

    assert result.action == "moderation_review"
    assert bot.deleted == []
    audit = db_session.scalar(select(ModerationAction))
    assert audit is not None
    assert audit.action == "review"
    assert audit.reason == "rules-v2:suspicious_direct_message_promo"


@pytest.mark.asyncio
async def test_same_group_message_is_not_deleted_twice_with_different_update_id(
    db_session: Session,
) -> None:
    bot = FakeBot()
    runtime = TelegramRuntime(
        db_session,
        bot,
        city="belgrade",
        staging_discussion_group_id="-100777",
    )
    payload = _group_message(302, 33, "100% доход без риска")

    first = await runtime.process(payload)
    payload["update_id"] = 303
    second = await runtime.process(payload)

    assert first.action == "moderation_delete"
    assert second.action == "moderation_delete"
    assert bot.deleted == [(-100777, 33)]
    assert db_session.scalar(select(func.count()).select_from(ModerationAction)) == 1
    assert db_session.scalar(select(func.count()).select_from(TelegramUpdateReceipt)) == 2
