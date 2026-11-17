from contextlib import nullcontext
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_owner_review_intake import OWNER, OWNER_CHAT, OwnerBot, _review
from test_private_intake import engine as _engine

from relocant_media.ops.intake_batch import collect_batch
from relocant_media.ops.serverless_queue import decode_result
from relocant_media.ops.serverless_snapshot import export_intake_state
from relocant_media.services.serverless_intake import import_inbox
from relocant_media.storage.models import Submission, TelegramUpdateReceipt

engine = _engine


def test_transfer_result_matches_pinned_cli() -> None:
    output = '> endpoints/transfer {"operation":"peek"}\n< {"status":"ready","events":[]}  9ms\n'
    assert decode_result(output) == {"status": "ready", "events": []}
    with pytest.raises(ValueError):
        decode_result('> endpoints/transfer\n✗ unauthorized\n')


@pytest.mark.asyncio
async def test_open_case_and_owner_callback_survive_serverless_replay(engine):
    at = datetime.now(UTC)
    media = {"kind": "photo", "file_id": "photo-123"}
    with Session(engine) as session:
        owner = _review(session)
        row = Submission(
            kind="news_tip", status="collecting", telegram_user_id=42, telegram_message_id=100,
            payload={"chat_id": 42, "reader_intake": True, "text": "Черновик",
                     "attachments": [media]},
        )
        session.add(row)
        session.commit()
        row_id = row.id
        owner_id = owner.id
        exported = export_intake_state(session, at)
    assert len(exported["cases"]) == 1
    assert exported["cases"][0]["reference"] == str(row_id)
    draft = exported["cases"][0]
    draft["status"] = "new"
    draft["payload"].update(public_reference=str(row_id), accepted_at=at.isoformat())
    events = [
        {
            "sequence": 1,
            "update": {
                "update_id": 501,
                "message": {"message_id": 101, "from": {"id": 42}, "chat": {"id": 42}},
            },
            "draft": draft,
            "notice": {"key": "accepted", "status": "sent", "telegram_message_id": 201},
        },
        {
            "sequence": 2,
            "update": {
                "update_id": 502,
                "callback_query": {
                    "id": "callback-1", "from": {"id": OWNER},
                    "data": f"or:publish:{owner_id}",
                    "message": {"message_id": 501, "chat": {"id": OWNER_CHAT}},
                },
            },
        },
    ]
    bot = OwnerBot([], callback_expired=True)
    for _ in range(2):
        assert await import_inbox(
            engine, bot, events, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT,
        ) == 2
    assert [call for call, _ in bot.calls].count("sendMessage") == 1
    assert not any(call == "getUpdates" for call, _ in bot.calls)
    with Session(engine) as session:
        assert session.get(Submission, owner_id).status == "approved"
        imported = session.get(Submission, row_id)
        assert imported.status == "new"
        assert imported.payload["attachments"] == [media]
        assert imported.payload["reader_notices"]["accepted"]["telegram_message_id"] == 201
        assert len(session.scalars(select(TelegramUpdateReceipt)).all()) == 2


def test_export_requires_resolved_source_receipts(engine):
    with Session(engine) as session:
        session.add(TelegramUpdateReceipt(update_id=700, kind="reader_intake", status="processing"))
        session.commit()
        with pytest.raises(ValueError, match="unresolved"):
            export_intake_state(session, datetime.now(UTC))


@pytest.mark.asyncio
async def test_batch_resumes_serverless_queue_at_checkpoint(engine):
    requested: list[int] = []
    state = {"serverless_sequence": 4}

    def load_events(after):
        requested.append(after)
        if after == 4:
            return [{"sequence": 5, "update": {"update_id": 999, "message": {}}}]
        return []

    bot = OwnerBot([])
    for _ in range(2):
        result = await collect_batch(
            engine, bot, enabled=True, store_guard=lambda: nullcontext(True),
            run_pipeline=lambda: pytest.fail("a discarded update must not start the pipeline"),
            state=state, reader_feedback_enabled=True, load_serverless_events=load_events,
        )
        assert result["status"] == "ready"
    assert requested == [4, 5]
    assert bot.calls == []
