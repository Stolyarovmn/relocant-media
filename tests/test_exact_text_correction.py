"""Точные исправления синтетических публикаций, без сети и рабочей среды."""

import copy
import hashlib
import html
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_correction_delivery import NOW, OLD, TOKEN, MemoryStorage, embed
from test_correction_delivery import frozen as base_frozen
from test_correction_delivery import ready_package as base_ready_package
from test_correction_delivery import seeded as base_seeded

from relocant_media.ai.package import canonical_json
from relocant_media.bot.formatting import editorial_caption
from relocant_media.ops import correction_delivery as worker
from relocant_media.ops.exact_text_correction import (
    ExactTextCorrectionPlan,
    prepared_exact_edit,
    source_text,
    text_sha256,
)
from relocant_media.storage.models import EditorialRun, Post, Source

seeded = base_seeded
ready_package = base_ready_package
frozen = base_frozen


class CasStorage(MemoryStorage):
    def __init__(self, state, blobs):
        super().__init__(state, blobs)
        self.writes = []
        self.checkpoints = []
        self.fail_claim = self.unknown_claim = self.fail_finish = False

    def upload(self, path, folder):
        if folder == "snapshot":
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            self.blobs[digest] = data
            return digest
        return super().upload(path, folder)

    def write(self, state, expected):
        assert expected == worker._digest(self.state)
        if state["status"] == "busy":
            assert self.state["status"] == "ready"
            if self.fail_claim:
                raise ValueError("CAS conflict")
        else:
            assert self.state["status"] == "busy"
            assert state["generation"] == self.state["attempt"]
            if self.fail_finish:
                raise ValueError("Final CAS failed")
        self.state = copy.deepcopy(state)
        self.writes.append(copy.deepcopy(state))
        if state["status"] == "busy" and self.unknown_claim:
            raise ValueError("Claim outcome unknown")
        return worker._digest(state)

    def checkpoint(self, state, expected, path):
        envelope = {"version": 1, "expected_busy_sha256": expected, "state": state}
        path.write_text(canonical_json(envelope), encoding="utf8")
        self.checkpoints.append(copy.deepcopy(envelope))
        return worker._digest(envelope)


def seal(storage, plan):
    context = worker._digest({key: value for key, value in plan.items() if key != "reviews"})
    for review in plan["reviews"]:
        review["context_sha256"] = context
    content = canonical_json(plan).encode()
    sha = text_sha256(content.decode())
    storage.blobs[sha] = content
    return sha


@pytest.fixture
def exact(frozen):
    engine, base, _, _, before = frozen
    storage = CasStorage(copy.deepcopy(base.state), copy.deepcopy(base.blobs))
    with Session(engine) as session:
        run = session.scalar(select(EditorialRun))
        post = session.get(Post, run.post_id)
        package = worker.EditorialPackage.model_validate_json(
            canonical_json(run.outcome["_package"]))
        source = package.presentation.illustration["source_url"]
        old, _ = editorial_caption(f"{post.title}\n\n{post.body}\n\nИсточник: {source}")
        sources = {doc.url: "<article>" + html.escape(doc.body) + "</article>"
                   for doc in package.event.documents}
        plan = {"version": 4, "city": "belgrade", "operation": "remove_unsupported_claims",
            "raw_item_id": run.raw_item_id, "post_id": post.id,
            "message_id": post.telegram_message_id,
            "old_package_sha256": run.outcome["_package_sha256"],
            "old_title_sha256": text_sha256(post.title), "old_body_sha256": text_sha256(post.body),
            "old_text_sha256": text_sha256(old), "title": post.title + " · исправлено",
            "body": post.body.replace(OLD + "\n\n", ""), "source_url": source,
            "removed_fragments": [OLD], "documents": [{
                "source_id": doc.source_id, "url": doc.url,
                "document_sha256": worker._digest(doc.model_dump(mode="json")),
                "live_text_sha256": text_sha256(source_text(sources[doc.url])),
                "quotes": [" ".join(doc.body.split())[:70]],
            } for doc in package.event.documents]}
    plan["reviews"] = [{"reviewer_id": f"independent-{role}", "role": role,
        "context_sha256": "0" * 64, "reviewed_at": NOW.isoformat(), "verdict": "approve",
        "score": 95, "flags": [], "live_source_checked": True,
        "scope": "remove_unsupported_claims_only", "factual_support": "supported",
        "no_new_sensitive_claims": True,
        "reason": "Удалено неподтверждённое утверждение, новых фактов и призывов не добавлено."}
        for role in ("facts", "utility")]
    sha = seal(storage, plan)
    return dict(engine=engine, storage=storage, plan=plan, sha=sha, sources=sources,
                old=old, database=Path(engine.url.database), before=before)


class Remote:
    def __init__(self, exact, failure=None):
        self.exact, self.failure = exact, failure
        self.current = embed(exact["old"], exact["plan"]["source_url"])
        self.posts = []

    def __call__(self, request):
        if request.method == "GET":
            if str(request.url) in self.exact["sources"]:
                return httpx.Response(200, text=self.exact["sources"][str(request.url)])
            assert request.url.host == "t.me"
            return httpx.Response(200, text=self.current)
        assert request.url.path.endswith("/editMessageText")
        storage = self.exact["storage"]
        assert storage.state["status"] == "busy" and storage.checkpoints
        assert storage.audits[-1]["operation"] == "editMessageText"
        self.posts.append(json.loads(request.content))
        payload = self.posts[-1]
        assert payload["message_id"] == self.exact["plan"]["message_id"]
        if self.failure == "timeout_before":
            raise httpx.ReadTimeout("Частный ответ", request=request)
        self.current = embed(payload["text"], self.exact["plan"]["source_url"])
        if self.failure == "timeout_after":
            raise httpx.ReadTimeout("Частный ответ", request=request)
        if self.failure == "readback":
            self.current = embed("Другой текст\n\nИсточник", self.exact["plan"]["source_url"])
        result = {"message_id": payload["message_id"],
            "chat": {"id": payload["chat_id"], "type": "channel"},
            "text": payload["text"], "entities": list(reversed(payload["entities"]))}
        if self.failure == "recipient":
            result["chat"]["id"] += 1
        if self.failure == "entities":
            result["entities"] = []
        return httpx.Response(200, json={"ok": True, "result": result})


def execute(exact, path, remote):
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        return worker.execute(exact["storage"], exact["sha"], path,
                              client, token=TOKEN, now=NOW)


def test_exact_correction_claims_cas_and_preserves_original_history(exact, tmp_path):
    remote = Remote(exact)
    report = execute(exact, tmp_path / "first", remote)
    assert report["status"] == "corrected" and len(remote.posts) == 1
    storage = exact["storage"]
    assert [state["status"] for state in storage.writes] == ["busy", "ready"]
    assert storage.audits[-1]["api_confirmed"] is storage.audits[-1]["public_confirmed"] is True
    assert next(iter(storage.state["exact_text_corrections"].values()))["attempts"] == 1
    assert storage.state["snapshot"] != storage.writes[0]["snapshot"]
    assert exact["database"].read_bytes() == exact["before"]
    replay = execute(exact, tmp_path / "replay", remote)
    assert replay["status"] == "already_corrected" and len(remote.posts) == 1
    assert len(storage.writes) == 2
    assert not set(report) & {"message_id", "raw_item_id", "body", "title"}


@pytest.mark.parametrize("failure", ["timeout_before", "timeout_after", "recipient", "entities",
                                     "readback"])
def test_unknown_result_is_durable_and_never_resends(exact, tmp_path, failure):
    remote = Remote(exact, failure)
    report = execute(exact, tmp_path / "first", remote)
    assert report["status"] == "uncertain"
    assert exact["storage"].state["status"] == "ready"
    replay = execute(exact, tmp_path / "replay", remote)
    assert replay["status"] == "uncertain" and len(remote.posts) == 1
    assert len(exact["storage"].writes) == 2
    assert exact["database"].read_bytes() == exact["before"]


@pytest.mark.parametrize("failure", ["fail_claim", "unknown_claim", "fail_finish"])
def test_cas_failures_never_authorize_another_edit(exact, tmp_path, failure):
    setattr(exact["storage"], failure, True)
    remote = Remote(exact)
    with pytest.raises(ValueError):
        execute(exact, tmp_path / "first", remote)
    assert len(remote.posts) == int(failure == "fail_finish")
    if failure != "fail_claim":
        with pytest.raises(ValueError, match="not ready"):
            execute(exact, tmp_path / "replay", remote)
        assert len(remote.posts) == int(failure == "fail_finish")
    checkpoint = exact["storage"].checkpoints[0]
    target = next(iter(checkpoint["state"]["exact_text_corrections"].values()))
    assert target["status"] == "uncertain"


@pytest.mark.parametrize("mutation", ["source", "public", "public_source", "state",
    "old_package_sha256", "old_title_sha256", "old_body_sha256", "old_text_sha256",
    "post_id", "message_id", "document_sha256", "live_text_sha256", "quote", "removed"])
def test_wrong_source_scope_hash_or_snapshot_blocks_before_claim(exact, tmp_path, mutation):
    plan = exact["plan"]
    remote = Remote(exact)
    if mutation == "source":
        exact["sources"][plan["source_url"]] = exact["sources"][plan["source_url"]].replace(
            "</article>", "Дополнение источника</article>")
    elif mutation == "public":
        remote.current = embed("Изменённый пост\n\nИсточник", plan["source_url"])
    elif mutation == "public_source":
        remote.current = embed(exact["old"], "https://different.example/source")
    elif mutation == "state":
        exact["storage"].change_state = True
    elif mutation in {"document_sha256", "live_text_sha256"}:
        plan["documents"][0][mutation] = "0" * 64
    elif mutation == "quote":
        plan["documents"][0]["quotes"] = ["Выдуманное доказательство отсутствует"]
    elif mutation == "removed":
        plan["removed_fragments"] = ["Нет такого фрагмента в исходном тексте"]
    elif mutation in {"post_id", "message_id"}:
        plan[mutation] += 1
    else:
        plan[mutation] = "0" * 64
    exact["sha"] = seal(exact["storage"], plan)
    with pytest.raises(ValueError):
        execute(exact, tmp_path / "blocked", remote)
    assert not remote.posts and not exact["storage"].writes


@pytest.mark.parametrize("mutation", ["identity", "role", "stale", "future", "flag", "scope",
                                     "sensitive", "support", "context", "category"])
def test_two_fresh_scope_reviews_are_required(exact, tmp_path, mutation):
    plan = exact["plan"]
    review = plan["reviews"][0]
    if mutation == "identity":
        review["reviewer_id"] = plan["reviews"][1]["reviewer_id"].upper()
    elif mutation == "role":
        review["role"] = "utility"
    elif mutation in {"stale", "future"}:
        delta = timedelta(hours=-3 if mutation == "stale" else 1)
        review["reviewed_at"] = (NOW + delta).isoformat()
    elif mutation == "flag":
        review["flags"] = ["Требуется проверка"]
    elif mutation == "scope":
        review["scope"] = "publish_new_material"
    elif mutation == "sensitive":
        review["no_new_sensitive_claims"] = False
    elif mutation == "support":
        review["factual_support"] = "unsupported"
    elif mutation == "category":
        plan["category"] = "transport"
    exact["sha"] = seal(exact["storage"], plan)
    if mutation == "context":
        review["context_sha256"] = "0" * 64
        content = canonical_json(plan).encode()
        exact["sha"] = text_sha256(content.decode())
        exact["storage"].blobs[exact["sha"]] = content
    remote = Remote(exact)
    with pytest.raises(ValueError):
        execute(exact, tmp_path / "blocked", remote)
    assert not remote.posts and not exact["storage"].writes


@pytest.mark.parametrize("protection", ["sending", "uncertain", "staging", "receipt", "disabled"])
def test_existing_delivery_and_source_guards_are_preserved(exact, protection):
    plan = ExactTextCorrectionPlan.model_validate_json(canonical_json(exact["plan"]))
    with Session(exact["engine"]) as session:
        run = session.scalar(select(EditorialRun))
        post = session.get(Post, run.post_id)
        if protection in {"sending", "uncertain"}:
            run.delivery_status = protection
        elif protection == "staging":
            run.staging_message_id = 123
        elif protection == "receipt":
            post.telegram_message_id = None
        else:
            session.get(Source, plan.documents[0].source_id).enabled = False
        with pytest.raises(ValueError):
            prepared_exact_edit(session, plan)


def test_new_plan_cannot_bypass_existing_target_intent(exact, tmp_path):
    remote = Remote(exact, "timeout_before")
    execute(exact, tmp_path / "first", remote)
    exact["plan"]["body"] += "\nУточнённая редакция."
    exact["sha"] = seal(exact["storage"], exact["plan"])
    with pytest.raises(ValueError, match="другое намерение"):
        execute(exact, tmp_path / "blocked", remote)
    assert len(remote.posts) == 1


def test_source_extraction_ignores_sidebars_and_rejects_ambiguous_articles():
    article = "<article><h1>Местная новость</h1><p>Подтверждённое описание события.</p>"
    first = article + "<aside>Первое объявление</aside></article><nav>Меню</nav>"
    second = article + "<aside>Другая реклама</aside></article><nav>Новые ссылки</nav>"
    assert source_text(first) == source_text(second)
    assert source_text(first) != source_text(first.replace("описание", "уточнение"))
    with pytest.raises(ValueError):
        source_text("<p>Нераспознанная страница без статьи</p>")
    with pytest.raises(ValueError):
        source_text(first + article + "</article>")
    n1 = ('<h1>Главная новость</h1><article class="article-wrapper"><p>Вводный текст.</p>'
          '<div class="article-content-wrapper"><div class="rich-text-block">'
          'Основной текст статьи.</div></div></article><aside>Реклама</aside>')
    assert source_text(n1) == source_text(n1.replace("Реклама", "Новая реклама"))
    with pytest.raises(ValueError):
        source_text(n1.replace('class="rich-text-block"', 'class="unknown-block"'))


def test_confirmed_overlay_is_one_effective_version_and_invalid_overlay_is_not_approved(
    exact, tmp_path,
):
    import sqlite3

    from sqlalchemy import create_engine

    from relocant_media.services.editorial_products import recent_publications
    from relocant_media.services.newsroom_control import build_newsroom_inbox
    from relocant_media.services.published_text import effective_published_text

    execute(exact, tmp_path / "first", Remote(exact))
    storage = exact["storage"]
    copy_db = tmp_path / "corrected.sqlite3"
    copy_db.write_bytes(storage.blobs[storage.state["snapshot"]["sha256"]])
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(copy_db))
    try:
        with Session(engine) as session:
            run = session.scalar(select(EditorialRun))
            post = session.get(Post, run.post_id)
            effective = effective_published_text(post, run)
            assert effective.corrected
            assert (effective.title, effective.body) == (
                exact["plan"]["title"], exact["plan"]["body"])
            assert post.title != effective.title and OLD in post.body
            assert run.delivery_status == "sent" and post.telegram_message_id is not None
            history = recent_publications(session, "belgrade", now=NOW)
            assert history[0]["title"] == effective.title and history[0]["body"] == effective.body
            assert build_newsroom_inbox(session)["items"][0]["title"] == effective.title
            original = copy.deepcopy(run.outcome["_published_correction"])
            for field in ("plan_sha256", "intent_sha256", "receipt_sha256"):
                run.outcome = {**run.outcome, "_published_correction": {
                    **original, field: "0" * 64}}
                assert not effective_published_text(post, run).corrected
            bad_receipt = {**original["receipt"], "api_confirmed": False}
            run.outcome = {**run.outcome, "_published_correction": {
                **original, "receipt": bad_receipt,
                "receipt_sha256": worker._digest(bad_receipt)}}
            assert not effective_published_text(post, run).corrected
            # Согласованные между собой hashes не заменяют канонического адресата.
            for target, value in (("chat_id", original["intent"]["chat_id"]+1),
                                  ("message_id", original["intent"]["message_id"]+1),
                                  ("operation", "sendMessage"), ("version", 3)):
                forged = copy.deepcopy(original)
                forged["intent"][target] = forged["receipt"][target] = value
                forged["intent_sha256"] = worker._digest(forged["intent"])
                forged["receipt"]["intent_sha256"] = forged["intent_sha256"]
                forged["receipt_sha256"] = worker._digest(forged["receipt"])
                run.outcome = {**run.outcome, "_published_correction": forged}
                assert not effective_published_text(post, run).corrected
    finally:
        engine.dispose()


def test_failed_commit_restores_checkpoint_without_resending(exact, tmp_path):
    exact["storage"].fail_finish = True
    remote = Remote(exact)
    with pytest.raises(ValueError):
        execute(exact, tmp_path / "first", remote)
    storage = exact["storage"]
    assert len(remote.posts) == 1 and storage.state["status"] == "busy"
    # Явная имитация ручного восстановления проверенной контрольной точки.
    checkpoint = storage.checkpoints[-1]["state"]
    assert next(iter(checkpoint["exact_text_corrections"].values()))["status"] == "corrected"
    storage.state = copy.deepcopy(checkpoint)
    assert execute(exact, tmp_path / "replay", remote)["status"] == "already_corrected"
    assert len(remote.posts) == 1
