import copy
import hashlib
import html
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy.orm import Session
from test_gpt_fallback_worker import ready_package as fallback_package
from test_server_cycle import seeded as server_seeded

from relocant_media.ai.package import canonical_json, context_sha256, draft_sha256
from relocant_media.ops import correction_delivery as worker
from relocant_media.ops.actions_runtime import bootstrap_record
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.storage.models import EditorialRun, Post

seeded = server_seeded
ready_package = fallback_package
NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)
OLD = "• 538 идёт из Бараева в сторону Меляка без заезда в Сремчицу."
NEW = "• 5538 идёт из Бараева в сторону Меляка без заезда в Сремчицу."
EVIDENCE_URL = "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151"
QUOTE = "Линија бр. 5538 Возила ће саобраћати од Барајева ка Мељаку без свраћања у Сремчицу."
TOKEN = "123456789:" + "FAKE_OFFLINE_TOKEN_" * 2


@pytest.mark.parametrize("receipt_id", [108, 109])
def test_digest_correction_edits_only_the_photo_caption_and_keyboard(tmp_path, receipt_id):
    slot = "belgrade:2026-11-14:morning"
    old = "<b>Белград сегодня · 14 ноября</b><br>Исходная подпись"
    url = "https://t.me/eduvbelgrad/102"
    caption = (
        '<b>Белград сегодня · 14 ноября</b>\n\nСвет <a href="' + url + '">Подробнее</a>\n'
        '<blockquote expandable>Адреса и время работ\n• Масарикова, 2</blockquote>'
    )
    map_url = ("https://t.me/relocant_media_belgrade_bot/map?startapp="
               "digest_event_90cd369f163d60459854-event_1a9d4a7a1e5be0b3f7da")
    plan = {
        "version": 2, "city": "belgrade", "message_id": 108, "slot": slot,
        "old_caption_sha256": worker._digest(worker._caption_signature(old)),
        "caption_html": caption, "map_url": map_url,
    }
    digest = worker._digest(plan)
    plan["reviews"] = [
        {"reviewer_id": role, "role": role, "context_sha256": digest,
         "reviewed_at": NOW.isoformat(), "verdict": "approve", "score": 95,
         "flags": [], "live_source_checked": True}
        for role in ("facts", "utility")
    ]
    content, sha = encode_plan(plan)
    state = {"digest_receipts": {slot: {"state": "sent", "message_id": receipt_id}}}
    storage = MemoryStorage(state, {sha: content})
    methods = []

    def respond(request):
        methods.append(request.method)
        if request.method == "GET":
            assert str(request.url) == "https://t.me/eduvbelgrad/108?embed=1"
            return httpx.Response(200, text=(
                '<div class="tgme_widget_message" data-post="eduvbelgrad/108">'
                f'<div class="tgme_widget_message_text">{old}</div>'
                '<a class="tgme_widget_message_photo_wrap"></a></div>'
            ))
        assert request.url.path.endswith("/editMessageCaption")
        payload = json.loads(request.content)
        assert payload["message_id"] == 108 and payload["caption"] == caption
        assert set(payload) == {"chat_id", "message_id", "caption", "parse_mode", "reply_markup"}
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": 108, "chat": {"id": payload["chat_id"]}, "photo": [{}],
            "caption": worker._caption_signature(caption)["text"],
            "caption_entities": [
                {"type": "text_link", "url": url}, {"type": "expandable_blockquote"},
            ],
            "reply_markup": payload["reply_markup"],
        }})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if receipt_id != 108:
            with pytest.raises(ValueError, match="квитанциях"):
                worker.execute(storage, sha, tmp_path / "edit", client, token=TOKEN, now=NOW)
            assert methods == []
        else:
            result = worker.execute(storage, sha, tmp_path / "edit", client, token=TOKEN, now=NOW)
            assert result["status"] == "corrected" and result["expandable_quotes"] == 1
            assert methods == ["GET", "POST"]


def plan_dict(raw_id=1):
    value = {"version": 1, "city": "belgrade", "raw_item_id": raw_id, "message_id": 94,
             "old_fragment": OLD, "new_fragment": NEW, "evidence_url": EVIDENCE_URL,
             "evidence_text": QUOTE}
    digest = worker._digest(value)
    value["reviews"] = [
        {"reviewer_id": f"independent-{role}", "role": role, "context_sha256": digest,
         "reviewed_at": NOW.isoformat(), "verdict": "approve", "score": 94,
         "flags": [], "live_source_checked": True}
        for role in ("facts", "utility")
    ]
    return value


def encode_plan(value):
    content = canonical_json(value).encode("utf-8")
    return content, hashlib.sha256(content).hexdigest()


class MemoryStorage:
    def __init__(self, state, blobs):
        self.state = state
        self.blobs = blobs
        self.audits = []
        self.reads = 0
        self.change_state = False

    def read(self):
        self.reads += 1
        value = copy.deepcopy(self.state)
        if self.change_state and self.reads > 1:
            value["generation"] = "another-generation"
        return value, worker._digest(value)

    def download(self, sha):
        return self.blobs[sha]

    def upload(self, path, folder):
        assert folder == "correction-audit"
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        assert self.blobs.get(digest, content) == content
        self.blobs[digest] = content
        self.audits.append(json.loads(content))
        return digest


def embed(text, url, *, media=False):
    assert text.endswith("Источник")
    content = html.escape(text[:-len("Источник")]).replace("\n", "<br>")
    content += f'<a href="{html.escape(url, quote=True)}">Источник</a>'
    attachment = '<div class="tgme_widget_message_photo_wrap"></div>' if media else ""
    return ('<div class="tgme_widget_message" data-post="eduvbelgrad/94">'
            f'<div class="tgme_widget_message_text">{content}</div>{attachment}</div>')


@pytest.fixture
def frozen(ready_package, tmp_path):
    engine, original = ready_package
    body = f"Учебное сообщение для проверки исправления.\n\n{OLD}\n\n#ГородМеняется"
    draft = original.outcome.draft.model_copy(update={"body": body})
    package = original.model_copy(update={
        "outcome": original.outcome.model_copy(update={"draft": draft})})
    package = package.model_copy(update={"reviews": tuple(
        review.model_copy(update={"context_sha256": context_sha256(package),
                                  "draft_sha256": draft_sha256(draft)})
        for review in package.reviews)})
    with Session(engine) as session:
        post = Post(city="belgrade", category="transport", status="ready", title=draft.title,
                    body=body, confidence=.99, telegram_message_id=94, published_at=NOW)
        session.add(post)
        session.flush()
        session.add(EditorialRun(
            raw_item_id=package.raw_item_id, post_id=post.id, delivery_status="sent",
            evidence=package.evidence.model_dump(mode="json"), outcome={
                "_package": package.model_dump(mode="json"),
                "_package_sha256": worker._digest(package.model_dump(mode="json"))}))
        session.commit()
        content, plan_sha = encode_plan(plan_dict(package.raw_item_id))
        plan = worker.validate_plan(content, plan_sha, now=NOW)
        edit = worker.prepared_edit(session, plan)
    database = Path(engine.url.database)
    snapshot = tmp_path / "snapshot"
    manifest = create_snapshot(database, snapshot)
    manifest["created_at"] = NOW.isoformat()
    manifest["drive_file_id"] = manifest["sha256"]
    state = bootstrap_record(manifest, now=NOW)
    blobs = {plan_sha: content, manifest["sha256"]: (snapshot / "editorial.sqlite3").read_bytes()}
    return engine, MemoryStorage(state, blobs), plan_sha, edit, database.read_bytes()


class PublicAndBot:
    def __init__(self, storage, edit):
        self.storage, self.edit = storage, edit
        self.current = embed(edit["old_text"], edit["old_source_url"])
        self.source = f"<article>{QUOTE}</article>"
        self.redirect = False
        self.timeout = False
        self.bad_result = None
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        if request.method == "GET" and str(request.url) == EVIDENCE_URL:
            if self.redirect:
                return httpx.Response(302, headers={"location": "https://example.org/"})
            return httpx.Response(200, text=self.source)
        if request.method == "GET" and str(request.url) == "https://t.me/eduvbelgrad/94?embed=1":
            return httpx.Response(200, text=self.current)
        assert request.method == "POST" and request.url.path.endswith("/editMessageText")
        assert self.storage.audits[-1]["operation"] == "editMessageText"
        payload = json.loads(request.content)
        assert payload == self.edit["payload"]
        self.current = embed(payload["text"], EVIDENCE_URL)
        if self.timeout:
            raise httpx.ReadTimeout(f"Private token: {TOKEN}", request=request)
        result = {"message_id": 94, "chat": {"id": payload["chat_id"], "type": "channel"},
                  "text": payload["text"], "entities": payload["entities"]}
        if self.bad_result:
            result.update(self.bad_result)
        return httpx.Response(200, json={"ok": True, "result": result})


def run(frozen, tmp_path, remote, directory="correction"):
    _, storage, plan_sha, _, _ = frozen
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        return worker.execute(storage, plan_sha, tmp_path / directory, client, token=TOKEN, now=NOW)


def test_exact_edit_preserves_database_and_uploads_intent_before_api(frozen, tmp_path, capsys):
    engine, storage, plan_sha, edit, original_bytes = frozen
    remote = PublicAndBot(storage, edit)
    result = run(frozen, tmp_path, remote)
    assert result["status"] == "corrected"
    assert [call.method for call in remote.calls] == ["GET", "GET", "POST"]
    assert edit["payload"]["text"] == edit["old_text"].replace(OLD, NEW, 1)
    assert edit["payload"]["entities"][0]["url"] == EVIDENCE_URL
    assert edit["payload"]["link_preview_options"]["url"] == EVIDENCE_URL
    assert storage.audits[0]["plan_sha256"] == plan_sha
    assert storage.audits[1]["intent_sha256"] == result["intent_sha256"]
    assert storage.audits[1]["status"] == "corrected"
    assert Path(engine.url.database).read_bytes() == original_bytes
    assert TOKEN not in capsys.readouterr().out


def test_unknown_api_outcome_is_reconciled_without_a_second_edit(frozen, tmp_path, capsys):
    _, storage, _, edit, _ = frozen
    remote = PublicAndBot(storage, edit)
    remote.timeout = True
    first = run(frozen, tmp_path, remote, "first")
    assert first["status"] == "uncertain"
    assert storage.audits[-1]["error_type"] == "ReadTimeout"
    second = run(frozen, tmp_path, remote, "second")
    assert second["status"] == "already_corrected"
    assert len([call for call in remote.calls if call.method == "POST"]) == 1
    assert storage.audits[-2]["visible_state"] == "corrected"
    assert TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize("mutation", [
    "hash", "duplicate_role", "duplicate_id", "low_score", "flag", "not_live", "stale",
    "future", "bool_version", "multiline", "words", "no_number", "unconfirmed_number",
    "external_source", "url_query", "url_credentials", "unknown_field",
])
def test_invalid_plan_is_rejected(mutation):
    value = plan_dict()
    review = value["reviews"][0]
    if mutation == "hash":
        review["context_sha256"] = "0" * 64
    elif mutation == "duplicate_role":
        review["role"] = "utility"
    elif mutation == "duplicate_id":
        review["reviewer_id"] = value["reviews"][1]["reviewer_id"].upper()
    elif mutation == "low_score":
        review["score"] = 74
    elif mutation == "flag":
        review["flags"] = ["не подтверждено"]
    elif mutation == "not_live":
        review["live_source_checked"] = False
    elif mutation in {"stale", "future"}:
        offset = timedelta(hours=-3 if mutation == "stale" else 1)
        review["reviewed_at"] = (NOW + offset).isoformat()
    elif mutation == "bool_version":
        value["version"] = True
    elif mutation == "multiline":
        value["new_fragment"] += "\nЕщё строка"
    elif mutation == "words":
        value["new_fragment"] = NEW.replace("Меляка", "центра")
    elif mutation == "no_number":
        value["new_fragment"] = "Строка без номера"
    elif mutation == "unconfirmed_number":
        value["new_fragment"] = OLD.replace("538", "999")
    elif mutation == "external_source":
        value["evidence_url"] = "https://example.org/linije/aktuelne-izmene/1151"
    elif mutation == "url_query":
        value["evidence_url"] += "?redirect=evil"
    elif mutation == "url_credentials":
        value["evidence_url"] = value["evidence_url"].replace("https://", "https://user@")
    else:
        value["skip_guard"] = True
    content, digest = encode_plan(value)
    with pytest.raises(ValueError):
        worker.validate_plan(content, digest, now=NOW)


def test_plan_blob_is_bounded_and_bound_to_sha():
    content, digest = encode_plan(plan_dict())
    with pytest.raises(ValueError):
        worker.validate_plan(content, "0" * 64, now=NOW)
    content += b" " * worker.MAX_PLAN_BYTES
    with pytest.raises(ValueError):
        worker.validate_plan(content, hashlib.sha256(content).hexdigest(), now=NOW)
    with pytest.raises(ValueError):
        worker.validate_plan(content, digest, now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("mutation", ["quote", "redirect", "text", "link", "media", "state"])
def test_live_mismatch_blocks_edit_before_intent(frozen, tmp_path, mutation):
    _, storage, _, edit, _ = frozen
    remote = PublicAndBot(storage, edit)
    if mutation == "quote":
        remote.source = f"<script>{QUOTE}</script><p>Ничего не подтверждено</p>"
    elif mutation == "redirect":
        remote.redirect = True
    elif mutation == "text":
        remote.current = embed("Изменение извне\n" + edit["old_text"], edit["old_source_url"])
    elif mutation == "link":
        remote.current = embed(edit["old_text"], EVIDENCE_URL)
    elif mutation == "media":
        remote.current = embed(edit["old_text"], edit["old_source_url"], media=True)
    else:
        storage.change_state = True
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        run(frozen, tmp_path, remote)
    assert storage.audits == []
    assert not any(call.method == "POST" for call in remote.calls)


@pytest.mark.parametrize("bad_result", [
    {"message_id": 95}, {"chat": {"id": 123, "type": "channel"}},
    {"text": "Другой текст"}, {"entities": []}, {"photo": []},
])
def test_api_must_confirm_exact_message_chat_text_and_entities(frozen, tmp_path, bad_result):
    _, storage, _, edit, _ = frozen
    remote = PublicAndBot(storage, edit)
    remote.bad_result = bad_result
    assert run(frozen, tmp_path, remote)["status"] == "uncertain"
    assert storage.audits[-1]["error_type"] == "ValueError"
    assert len([call for call in remote.calls if call.method == "POST"]) == 1


@pytest.mark.parametrize("mutation", ["receipt", "status", "text", "hash", "media"])
def test_database_must_match_signed_package_and_sent_receipt(frozen, mutation):
    engine, storage, plan_sha, _, _ = frozen
    plan = worker.validate_plan(storage.download(plan_sha), plan_sha, now=NOW)
    with Session(engine) as session:
        run_row = session.get(EditorialRun, plan.raw_item_id)
        post = session.get(Post, run_row.post_id)
        if mutation == "receipt":
            post.telegram_message_id = 95
        elif mutation == "status":
            run_row.delivery_status = "pending"
        elif mutation == "text":
            post.body += "Изменённый текст"
        else:
            outcome = dict(run_row.outcome)
            outcome["_package_sha256" if mutation == "hash" else "_media_delivery"] = "changed"
            run_row.outcome = outcome
        session.flush()
        with pytest.raises(ValueError):
            worker.prepared_edit(session, plan)


def test_cli_refuses_non_manual_environment_without_network(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(sys, "argv", ["correction_delivery", "--plan-sha", "0" * 64,
                                     "--work", str(tmp_path / "work")])
    monkeypatch.setattr(worker, "SSHStateClient", lambda *args: pytest.fail("SSH запрещён"))
    with pytest.raises(SystemExit, match="1"):
        worker.main()
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
