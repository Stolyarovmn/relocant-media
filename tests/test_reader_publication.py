import json
import shutil
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.services.private_intake import process_private_updates
from relocant_media.services.reader_forwarded_source import public_forward_urls
from relocant_media.services.reader_publication import (
    POLICY,
    TERMS_SHA256,
    material_fingerprint,
    permission_reason,
    public_reader_material,
    reader_credit,
)
from relocant_media.services.reader_resolution import (
    process_reader_resolutions,
    record_reader_resolution_callback,
)
from relocant_media.services.serverless_intake import import_inbox
from relocant_media.storage.models import Base, Submission

OWNER = 3002000011


def material(*, mode="username", permission="allow"):
    evidence = {"message_id": 7, "at": "2026-11-18T16:00:00.000Z"}
    signature = {"mode": mode, "label": "" if mode == "anonymous" else "@reader_alias",
                 "evidence": dict(evidence)}
    if mode != "anonymous":
        signature["disclosure"] = {**evidence, "channel_id": POLICY["channel_id"],
                                  "label": signature["label"]}
    payload = {"city": "belgrade", "reader_intake": True, "chat_id": 42,
               "text": "Тест, не публиковать", "attachments": [
                   {"kind": "photo", "file_id": "photo", "file_unique_id": "photo-unique"}]}
    payload["reader_publication"] = {
        "version": POLICY["version"], "stage": "confirmed", "permission": permission,
        "ownership": "own", "terms": POLICY["terms"], "terms_sha256": TERMS_SHA256,
        "material_sha256": material_fingerprint(payload),
        "scope": {"channel_id": POLICY["channel_id"], "channel_url": POLICY["channel_url"]},
        "ownership_evidence": dict(evidence), "permission_evidence": dict(evidence),
        "confirmed": dict(evidence), "signature": signature,
    }
    return payload


def test_policy_is_identical_in_cloud_and_packaged_runtime():
    cloud = Path("deploy/vercel-intake/lib/reader-publication-policy.json")
    assert json.loads(cloud.read_text("utf-8")) == POLICY


@pytest.mark.parametrize("mode", ["anonymous", "username"])
def test_projection_needs_exact_human_review_and_never_exports_private_identity(mode):
    payload = material(mode=mode)
    payload.update(telegram_user_id=42, chat_id=42, first_name="Не публиковать")
    # Unreviewed files never ship, but no longer block the permitted text (owner 23.11.2026).
    text_only = public_reader_material(
        payload, target_channel_id=POLICY["channel_id"], owner_user_id=OWNER)
    assert text_only["attachments"] == []
    assert text_only["text"] == payload["text"]
    photo_only = {**payload, "text": ""}
    photo_only["reader_publication"] = {**payload["reader_publication"],
                                        "material_sha256": material_fingerprint(photo_only)}
    with pytest.raises(ValueError, match="ручной"):
        public_reader_material(photo_only, target_channel_id=POLICY["channel_id"],
                               owner_user_id=OWNER)
    payload["reader_media_review"] = {"decision": "approved", "owner_user_id": OWNER,
                                     "at": datetime.now(UTC).isoformat(),
                                     "material_sha256": material_fingerprint(payload)}
    public = public_reader_material(
        payload, target_channel_id=POLICY["channel_id"], owner_user_id=OWNER,
    )
    assert set(public) == {"text", "attachments", "credit", "material_sha256"}
    expected_credit = "" if mode == "anonymous" else "Фото: @reader_alias"
    assert public["attachments"][0]["credit"] == expected_credit
    payload["attachments"].append({"kind": "video", "file_id": "new", "file_unique_id": "new"})
    assert permission_reason(payload, POLICY["channel_id"])


@pytest.mark.parametrize("change", ["channel", "terms", "source", "disclosure", "private", "text"])
def test_permission_does_not_expand_silently(change):
    payload = material()
    r = payload["reader_publication"]
    if change == "channel":
        r["scope"]["channel_id"] = -10012345
    elif change == "terms":
        r["terms"] += " И другие каналы."
    elif change == "source":
        r["ownership"] = "source"
    elif change == "disclosure":
        r["signature"].pop("disclosure")
    elif change == "private":
        r["permission"] = "private"
    else:
        payload["text"] += " Новые сведения."
    assert permission_reason(payload, POLICY["channel_id"])
    assert reader_credit(payload, kind="photo") == ""


def test_sender_with_author_permission_is_not_credited_as_photographer():
    payload = material()
    payload["reader_publication"]["ownership"] = "authorized"
    assert reader_credit(payload, kind="photo") == "Прислал: @reader_alias"


@pytest.mark.asyncio
@pytest.mark.parametrize("forwarded", [False, True])
async def test_js_dialogue_import_keeps_fingerprint_and_sends_no_second_messages(forwarded):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Для проверки контракта приёмника нужен Node.js.")
    script = """
import {fixture,message,seed} from './deploy/vercel-intake/test/helpers.js';
import {PUBLICATION_POLICY as P} from './deploy/vercel-intake/lib/reader-publication.js';
const f=await fixture({seed:seed({channel_id:P.channel_id})});
let id=0;
const send=(text, extra={})=>f.receive(message(++id,text,{
  from:{id:42,username:'reader_alias'}, ...extra,
}));
const forwarded=process.argv[1]==='forwarded';
await send('/start');
await send('Тест, не публиковать', forwarded ? {forward_origin:{
  type:'channel', chat:{type:'channel',username:'Serbia'}, message_id:197
}} : {});
await send('Предложить новость');
await send(forwarded ? 'Чужой источник' : 'Мой материал');
if (!forwarded) {
  await send('Разрешить публикацию'); await send('Мой @username');
  await send('Публиковать эту подпись');
}
await send('Отправить №'+(await f.state()).cases[0].reference);
await send('Разрешить публикацию');
console.log(JSON.stringify((await f.ingress.transfer({operation:'peek'})).events));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, "forwarded" if forwarded else "own"],
        check=True,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    events = json.loads(result.stdout)
    for entry in events:
        if isinstance(entry.get('draft'), dict):
            entry['draft']['payload']['public_source_review'] = {
                'owner_user_id': OWNER, 'at': '2026-11-18T16:00:00+00:00',
                'material_sha256': 'a' * 64,
            }
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        bot = Bot()
        for _ in range(2):
            await import_inbox(engine, bot, events, owner_user_id=None, owner_chat_id=None)
        with Session(engine) as session:
            row = session.scalar(select(Submission).where(Submission.kind == "news_tip"))
            assert row.status == "reader_review"
            assert 'public_source_review' not in row.payload
            receipt = row.payload["reader_publication"]
            assert receipt["material_sha256"] == material_fingerprint(row.payload)
            if forwarded:
                assert public_forward_urls(row.payload) == ["https://t.me/serbia/197"]
                assert receipt["permission"] == "private"
            else:
                assert permission_reason(row.payload, POLICY["channel_id"]) is None
                assert receipt["signature"]["label"] == "@reader_alias"
            assert len(list(session.scalars(select(Submission)))) == 1
        assert bot.sent == []
    finally:
        engine.dispose()


class Bot:
    def __init__(self):
        self.sent = []

    async def _call(self, method, payload):
        if method == "answerCallbackQuery":
            return True
        assert method in {"sendMessage", "sendPhoto", "editMessageText", "editMessageReplyMarkup"}
        self.sent.append((method, deepcopy(payload)))
        return {"message_id": payload.get("message_id", 50 + len(self.sent))}


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "description", "expected"), [
    ("timeout", "", "cleared"),
    ("http_retry", "Bad Request: message is not modified", "cleared"),
    ("not_modified", "Bad Request: message is not modified", "cleared"),
    ("http", "Bad Request: message is not modified: reply markup is the same", "cleared"),
    ("http", "Bad Request: message to edit not found", "unavailable"),
    ("http", "Bad Request: message can't be edited", "unavailable"),
    (None, "", "cleared"),
])
async def test_closed_keyboard_reconciles_without_repeating_final_answer(
    failure, description, expected,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    class KeyboardBot(Bot):
        attempts = 0

        async def _call(self, method, payload):
            if method == "editMessageReplyMarkup":
                self.attempts += 1
                if self.attempts <= 2 and failure == "timeout":
                    raise TimeoutError("test")
                if failure == "not_modified":
                    raise TelegramAPIError(description)
                if failure == "http" or (failure == "http_retry" and self.attempts <= 2):
                    code = 400 if failure == "http" else 503
                    response = httpx.Response(
                        code, json={"ok": False, "error_code": code, "description": description},
                        request=httpx.Request("POST", "https://example.org/editMessageReplyMarkup"),
                    )
                    return TelegramBotAPI._result(method, response)
            return await super()._call(method, payload)

    try:
        with Session(engine) as session:
            payload = material()
            payload["reader_resolution"] = {
                "owner_notice": {"status": "sent", "chat_id": OWNER, "telegram_message_id": 51},
                "final": {"status": "sent", "telegram_message_id": 60},
            }
            row = Submission(kind="news_tip", status="closed", telegram_user_id=42,
                             telegram_message_id=7, payload=payload)
            session.add(row)
            session.commit()
            sid = row.id
        bot = KeyboardBot()
        owner = {"owner_user_id": OWNER, "owner_chat_id": OWNER}
        await process_reader_resolutions(engine, bot, enabled=True, **owner)
        retry_delays = (2, 10) if failure in {"timeout", "http_retry"} else ()
        for attempts, minutes in enumerate(retry_delays, 1):
            await process_reader_resolutions(engine, bot, enabled=True, **owner)
            assert bot.attempts == attempts
            with Session(engine) as session:
                row = session.get(Submission, sid)
                state = deepcopy(row.payload["reader_resolution"])
                receipt = state["owner_keyboard"]
                assert receipt["status"] == "retry"
                assert receipt["attempts"] == attempts
                if failure == "http_retry":
                    assert receipt["http_status"] == 503
                attempted_at = datetime.fromisoformat(receipt["attempted_at"])
                retry_at = datetime.fromisoformat(receipt["retry_at"])
                assert retry_at - attempted_at == timedelta(minutes=minutes)
                receipt["retry_at"] = "2020-01-01T00:00:00+00:00"
                row.payload = {**row.payload, "reader_resolution": state}
                session.commit()
            await process_reader_resolutions(engine, bot, enabled=True, **owner)
        with Session(engine) as session:
            row = session.get(Submission, sid)
            assert row.payload["reader_resolution"]["owner_keyboard"]["status"] == expected
        before = bot.attempts
        await process_reader_resolutions(engine, bot, enabled=True, **owner)
        assert bot.attempts == before
        assert not any(method == "sendMessage" for method, _ in bot.sent)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_fenced_relay_preserves_permission_and_custom_signature():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    bot = Bot()
    mid = 0

    async def send(text):
        nonlocal mid
        mid += 1
        await process_private_updates(engine, bot, [{"update_id": mid, "message": {
            "message_id": mid, "date": int(datetime.now(UTC).timestamp()), "text": text,
            "from": {"id": 42, "username": "reader_alias"},
            "chat": {"id": 42, "type": "private"},
        }}], reader_feedback_enabled=True, publication_enabled=True)

    try:
        for text in ("/start", "Тест резервного пути, не публиковать", "Предложить новость",
                     "Мой материал", "Разрешить публикацию", "Имя / псевдоним", "Псевдоним",
                     "Публиковать эту подпись"):
            await send(text)
            if text == "Псевдоним":
                assert "обычным текстом без ссылки" in bot.sent[-1][1]["text"]
                assert "@username" not in bot.sent[-1][1]["text"]
        with Session(engine) as session:
            row = session.scalar(select(Submission))
            ref = row.payload["public_reference"]
            assert row.status == "collecting"
        await send(f"Отправить №{ref}")
        with Session(engine) as session:
            row = session.scalar(select(Submission))
            assert row.status == "reader_review"
            assert permission_reason(row.payload, POLICY["channel_id"]) is None
            assert row.payload["reader_publication"]["signature"]["label"] == "Псевдоним"
            assert row.payload["text"] == "Тест резервного пути, не публиковать"
        assert ref in bot.sent[-1][1]["text"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_moderation_error_is_a_persistent_message_without_removing_buttons():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            row = Submission(kind="news_tip", status="reader_review", telegram_user_id=42,
                             telegram_message_id=7, payload=material())
            session.add(row)
            session.commit()
            sid = row.id
        bot = Bot()
        owner = {"owner_user_id": OWNER, "owner_chat_id": OWNER}
        await process_reader_resolutions(engine, bot, enabled=True, **owner)
        await process_private_updates(engine, bot, [{"update_id": 90, "callback_query": {
            "id": "expired-callback", "data": f"rr:approve_media:{sid}",
            "from": {"id": OWNER}, "message": {"message_id": 51, "chat": {"id": OWNER}},
        }}], reader_feedback_enabled=True, **owner)
        assert "Сначала получите" in bot.sent[-1][1]["text"]
        assert not any(method == "editMessageReplyMarkup" for method, _ in bot.sent)
        with Session(engine) as session:
            assert not session.get(Submission, sid).payload.get("reader_media_review")
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_owner_gets_reader_material_without_primary_and_only_owner_can_moderate():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            row = Submission(kind="news_tip", status="reader_review", telegram_user_id=42,
                             telegram_message_id=7, payload=material())
            session.add(row)
            session.commit()
            sid = row.id
        bot = Bot()
        owner = {"owner_user_id": OWNER, "owner_chat_id": OWNER}
        await process_reader_resolutions(engine, bot, enabled=True, **owner)
        assert "Подпись: @reader_alias" in bot.sent[0][1]["text"]
        callback = {"data": f"rr:approve_media:{sid}", "from": {"id": OWNER},
                    "message": {"message_id": 51, "chat": {"id": OWNER}}}
        with Session(engine) as session:
            result = record_reader_resolution_callback(session, callback, **owner)
            assert "Сначала получите" in result.answer_text
            assert not session.get(Submission, sid).payload.get("reader_media_review")
            callback["data"] = f"rr:media:{sid}"
            record_reader_resolution_callback(session, callback, **owner)
            session.commit()
        await process_reader_resolutions(engine, bot, enabled=True, **owner)
        with Session(engine) as session:
            callback["data"] = f"rr:approve_media:{sid}"
            callback["from"]["id"] = 43
            assert record_reader_resolution_callback(session, callback, **owner) is None
            callback["from"]["id"] = OWNER
            result = record_reader_resolution_callback(session, callback, **owner)
            assert result.accepted
            session.commit()
            row = session.get(Submission, sid)
            assert public_reader_material(row.payload, target_channel_id=POLICY["channel_id"],
                                          owner_user_id=OWNER)
            assert row.status == "reader_review"
            assert not session.scalar(select(Submission).where(Submission.status == "closed"))
        assert all(method != "publish" for method, _ in bot.sent)
    finally:
        engine.dispose()
