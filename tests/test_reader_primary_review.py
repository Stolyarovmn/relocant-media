from copy import deepcopy

import pytest
from sqlalchemy.orm import Session
from test_reader_publication import POLICY, material
from test_reader_resolution import OWNER_ID, Bot, callback, submission
from test_reader_resolution import engine as source_engine

from relocant_media.services.reader_publication import material_fingerprint, permission_reason
from relocant_media.services.reader_resolution import (
    process_reader_resolutions,
    record_reader_resolution_callback,
)
from relocant_media.services.submission_queue import batch_submissions, tip_urls
from relocant_media.storage.models import Submission

engine = source_engine


@pytest.mark.asyncio
@pytest.mark.parametrize('initial_status', ['reader_review', 'waiting_primary'])
async def test_source_lead_enters_public_pipeline_without_grant_or_media_approval(
    engine, initial_status,
):
    payload = material(permission="private", mode="anonymous")
    payload["text"] = "Редакционный сигнал https://www.cukarica.rs/vest/123-event/"
    payload["reader_publication"].update(
        ownership="source", material_sha256=material_fingerprint(payload),
    )
    sid = submission(engine, status=initial_status, **payload)
    bot = Bot()
    owner = {"owner_user_id": OWNER_ID, "owner_chat_id": OWNER_ID}
    await process_reader_resolutions(engine, bot, enabled=True, **owner)
    buttons = bot.sent[-1][1]["reply_markup"]["inline_keyboard"]
    assert buttons[0][0]["callback_data"] == f"rr:primary:{sid}"
    assert ('Повторить' in buttons[0][0]['text']) == (initial_status == 'waiting_primary')
    with Session(engine) as session:
        assert not batch_submissions(session)
        action = record_reader_resolution_callback(session, callback(sid, "primary"), **owner)
        assert action.accepted
        session.commit()
        row = session.get(Submission, sid)
        assert row.status == "new"
        assert batch_submissions(session)[0].id == sid
        assert tip_urls(row.payload) == ["https://www.cukarica.rs/vest/123-event/"]
        assert permission_reason(row.payload, POLICY["channel_id"])
        assert "reader_media_review" not in row.payload
        assert row.payload["attachments"] == payload["attachments"]
        assert not record_reader_resolution_callback(
            session, callback(sid, "primary"), **owner,
        ).accepted
        changed = deepcopy(row.payload)
        changed["text"] += " https://other.example/new-private-link"
        assert not tip_urls(changed)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "card", "closed", "own", "changed", "no_url"])
async def test_primary_review_cannot_be_forged_or_used_for_other_material(engine, change):
    payload = material(permission="private", mode="anonymous")
    payload["text"] = "https://www.cukarica.rs/vest/123-event/"
    payload["reader_publication"].update(
        ownership="source", material_sha256=material_fingerprint(payload),
    )
    sid = submission(engine, status="reader_review", **payload)
    owner = {"owner_user_id": OWNER_ID, "owner_chat_id": OWNER_ID}
    await process_reader_resolutions(engine, Bot(), enabled=True, **owner)
    update = callback(sid, "primary")
    with Session(engine) as session:
        row = session.get(Submission, sid)
        original = deepcopy(row.payload)
        if change == "owner":
            update["from"]["id"] += 1
        elif change == "card":
            update["message"]["message_id"] += 1
        elif change == "closed":
            row.status = "closed"
        else:
            altered = deepcopy(row.payload)
            if change == "own":
                altered["reader_publication"]["ownership"] = "own"
            elif change == "changed":
                altered["text"] += " изменение"
            else:
                altered["text"] = "Без ссылки"
                altered["reader_publication"]["material_sha256"] = material_fingerprint(altered)
            row.payload = altered
        result = record_reader_resolution_callback(session, update, **owner)
        assert result is None or not result.accepted
        assert "public_source_review" not in row.payload
        assert row.payload.get("reader_media_review") == original.get("reader_media_review")


@pytest.mark.asyncio
async def test_existing_source_card_gets_new_action_without_new_reader_message(engine):
    payload = material(permission="private", mode="anonymous")
    payload["text"] = "https://www.cukarica.rs/vest/123-event/"
    payload["reader_publication"].update(
        ownership="source", material_sha256=material_fingerprint(payload),
    )
    payload["reader_resolution"] = {"owner_notice": {
        "status": "sent", "chat_id": OWNER_ID, "telegram_message_id": 51,
        "content_sha256": "legacy-card", "sent_at": "2026-11-18T16:00:00+00:00",
    }}
    sid = submission(engine, status="reader_review", **payload)
    owner = {"owner_user_id": OWNER_ID, "owner_chat_id": OWNER_ID}
    bot = Bot()
    await process_reader_resolutions(engine, bot, enabled=True, **owner)
    assert len(bot.sent) == 1 and bot.sent[0][0] == "editMessageText"
    edited = bot.sent[0][1]
    assert edited["message_id"] == 51
    assert edited["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"rr:primary:{sid}"
    await process_reader_resolutions(engine, bot, enabled=True, **owner)
    assert len(bot.sent) == 1
