from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.reader_resolution import (
    process_reader_resolutions,
    record_reader_resolution_callback,
    record_reader_resolution_reply,
)
from relocant_media.services.submission_feedback import (
    reply_to_submissions,
    resolve_submission,
    save_requested_followup,
)
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source, Submission

OWNER_ID = 3_002_000_011
READER_ID = 12
NATIVE_CHAT = -2000000000011


@pytest.fixture
def engine():
    value = create_engine("sqlite://")
    Base.metadata.create_all(value)
    yield value
    value.dispose()


class Bot:
    def __init__(self, *, offline=False, fail_send=False, wrong_parent=False):
        self.offline = offline
        self.fail_send = fail_send
        self.wrong_parent = wrong_parent
        self.sent = []

    async def _call(self, method, payload):
        if method == "getChat":
            if self.offline:
                raise httpx.ReadTimeout("network")
            return {"is_direct_messages": True, "parent_chat": {
                "id": 1 if self.wrong_parent else APPROVED_CHANNEL_ID,
            }}
        assert method in ("sendMessage", "sendPhoto", "sendVideo")
        self.sent.append((method, payload))
        if self.fail_send:
            raise httpx.ReadTimeout("network")
        return {"message_id": 50 + len(self.sent)}


def submission(engine, *, kind="news_tip", status="new", native=False, **extra):
    now = datetime.now(UTC)
    payload = {
        "city": "belgrade",
        "reader_intake": True,
        "chat_id": READER_ID,
        "text": "Закрытое обращение",
        "received_at": (now - timedelta(seconds=12)).isoformat(),
        "accepted_at": (now - timedelta(seconds=7)).isoformat(),
        **extra,
    }
    if native:
        payload.update(
            chat_id=NATIVE_CHAT, channel_id=APPROVED_CHANNEL_ID, topic_id=17,
            origin="channel_direct_messages",
        )
    with Session(engine) as session:
        row = Submission(kind=kind, status=status, telegram_user_id=READER_ID,
                         telegram_message_id=5, payload=payload)
        session.add(row)
        session.flush()
        row_id = row.id
        session.commit()
        return row_id


def accepted_tip(engine, *, delivery="sent", native=False, duplicate=False):
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(Source(
            id="test", city="belgrade", name="Организатор", url="https://example.org",
            source_type="official", trust=Decimal("1"),
        ))
        session.flush()
        original = RawItem(source_id="test", external_id="1", url="https://example.org/1",
                           title="Анонс", body="Подробности", published_at=now)
        post = Post(
            city="belgrade", category="transport", status="published", confidence=Decimal("1"),
            title="Анонс", body="Подробности", telegram_message_id=90, published_at=now,
        )
        session.add_all([original, post])
        session.flush()
        session.add(EditorialRun(
            raw_item_id=original.id, post_id=post.id, delivery_status=delivery,
            outcome={"status": "ready"}, evidence={},
        ))
        raw_id = original.id
        if duplicate:
            other = RawItem(
                source_id="test", external_id="2", url="https://example.org/related",
                canonical_url=original.url, title=original.title, body=original.body,
                published_at=now,
            )
            session.add(other)
            session.flush()
            raw_id = other.id
        session.commit()
    return submission(engine, status="source_linked", native=native, raw_item_ids=[raw_id])


def callback(row_id, which, message_id=51, owner=OWNER_ID):
    return {
        "data": f"rr:{which}:{row_id}",
        "from": {"id": owner, "is_bot": False},
        "message": {"chat": {"id": OWNER_ID}, "message_id": message_id},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("native", "duplicate"), [(False, False), (True, False), (True, True)])
async def test_final_news_has_link_from_confirmed_receipt(engine, native, duplicate):
    row_id = accepted_tip(engine, native=native, duplicate=duplicate)
    reference = "RM-7K4M-3D8Q" if native else str(row_id)
    if native:
        with Session(engine) as session:
            row = session.get(Submission, row_id)
            row.payload = {**row.payload, "public_reference": reference}
            session.commit()
    bot = Bot()
    result = await process_reader_resolutions(engine, bot, enabled=True)
    assert result["sent"] == 1 and result["response_seconds"][0] >= 7
    assert (await process_reader_resolutions(engine, bot, enabled=True))["sent"] == 0
    assert len(bot.sent) == 1
    _, out = bot.sent[0]
    assert out["chat_id"] == (NATIVE_CHAT if native else READER_ID)
    if native:
        assert out["direct_messages_topic_id"] == 17
    assert f"№{reference}" in out["text"] and "https://t.me/eduvbelgrad/90" in out["text"]
    with Session(engine) as session:
        row = session.get(Submission, row_id)
        assert row.status == "closed"
        final = row.payload["reader_resolution"]["final"]
        assert final["user_wait_seconds"] >= 12
        assert final["accepted_wait_seconds"] >= 7


@pytest.mark.asyncio
async def test_uncertain_publication_only_reaches_owner(engine):
    row_id = accepted_tip(engine, delivery="uncertain")
    bot = Bot()
    result = await process_reader_resolutions(
        engine, bot, enabled=True, owner_user_id=OWNER_ID, owner_chat_id=OWNER_ID,
    )
    assert result["owner_sent"] == 1 and result["sent"] == 0
    assert bot.sent[0][1]["chat_id"] == OWNER_ID
    with Session(engine) as session:
        assert session.get(Submission, row_id).status == "source_linked"


@pytest.mark.asyncio
async def test_owner_can_view_attachments_and_send_exact_reply(engine):
    row_id = submission(engine, kind="complaint", native=True, attachments=[
        {"kind": "photo", "file_id": "photo-file"},
        {"kind": "video", "file_id": "video-file"},
    ])
    bot = Bot()
    owner = {"owner_user_id": OWNER_ID, "owner_chat_id": OWNER_ID}
    await process_reader_resolutions(engine, bot, enabled=True, **owner)
    with Session(engine) as session:
        assert record_reader_resolution_callback(
            session, callback(row_id, "media", owner=READER_ID), **owner,
        ) is None
        assert not record_reader_resolution_callback(
            session, callback(row_id, "media", message_id=999), **owner,
        ).accepted
        assert record_reader_resolution_callback(
            session, callback(row_id, "media"), **owner,
        ).accepted
        session.commit()
    assert (await process_reader_resolutions(engine, bot, enabled=True, **owner))["media_sent"] == 2
    assert [entry[0] for entry in bot.sent] == ["sendMessage", "sendPhoto", "sendVideo"]
    assert {entry[1]["chat_id"] for entry in bot.sent} == {OWNER_ID}
    with Session(engine) as session:
        answer = {
            "from": {"id": OWNER_ID}, "chat": {"id": OWNER_ID},
            "reply_to_message": {"message_id": 51}, "text": "Комментарий проверили и удалили.",
        }
        assert record_reader_resolution_reply(session, answer, **owner).accepted
        session.commit()
        assert not record_reader_resolution_callback(session, callback(row_id, "close"), **owner
                                                     ).accepted
    assert (await process_reader_resolutions(engine, bot, enabled=True, **owner))["sent"] == 1
    _, params = bot.sent[-1]
    assert params["chat_id"] == NATIVE_CHAT and params["direct_messages_topic_id"] == 17
    assert "Комментарий проверили и удалили." in params["text"]


@pytest.mark.asyncio
async def test_native_destination_failure_only_retries_before_sending(engine):
    row_id = submission(engine, status="expired", native=True)
    bot = Bot(offline=True, fail_send=True)
    assert (await process_reader_resolutions(engine, bot, enabled=True))["destination_unavailable"]
    assert not bot.sent
    bot.offline = False
    assert (await process_reader_resolutions(engine, bot, enabled=True))["uncertain"]
    assert (await process_reader_resolutions(engine, bot, enabled=True))["unresolved"]
    assert len(bot.sent) == 1
    with Session(engine) as session:
        row = session.get(Submission, row_id)
        assert row.status == "closed"
        assert row.payload["reader_resolution"]["final"]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_expiry_is_only_sent_once_and_mismatched_channel_is_blocked(engine):
    submission(engine, status="expired", triage_reason="expired")
    submission(engine, status="expired", native=True, triage_reason="expired")
    bot = Bot(wrong_parent=True)
    assert (await reply_to_submissions(engine, bot, enabled=True))["sent"] == 0
    result = await process_reader_resolutions(engine, bot, enabled=True)
    assert result["sent"] == 1 and result["blocked"] == 1
    assert "Обращение закрыто." in bot.sent[0][1]["text"]
    await process_reader_resolutions(engine, bot, enabled=True)
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_clarification_has_number_and_latency_and_legacy_leaves_reply_alone(engine):
    row_id = submission(engine)
    with Session(engine) as session:
        row = session.get(Submission, row_id)
        resolve_submission(row, [], now=datetime.now(UTC))
        session.commit()
    bot = Bot()
    result = await reply_to_submissions(engine, bot, enabled=True)
    assert result["sent"] == 1 and result["response_seconds"][0] >= 7
    assert f"№{row_id}" in bot.sent[0][1]["text"]
    with Session(engine) as session:
        row = session.get(Submission, row_id)
        receipt = row.payload["feedback"]["missing_primary"]
        assert receipt["user_wait_seconds"] >= 12 and receipt["accepted_wait_seconds"] >= 7
        assert not save_requested_followup(session, {
            "reply_to_message": {"message_id": 51}, "from": {"id": READER_ID},
            "chat": {"id": READER_ID}, "message_id": 6, "text": "Нашёл первоисточник.",
        })
