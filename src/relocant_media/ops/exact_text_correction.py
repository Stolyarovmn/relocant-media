"""Одно проверенное удаление неподтверждённых утверждений из уже отправленного текста.

Новый план не создаёт право публикации, не меняет категорию/пакет и не добавляет
новый автоматический маршрут чувствительных новостей. Область исправления
подтверждают два независимых ревью точного плана, а не эвристика по словам.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json, context_sha256, draft_sha256
from relocant_media.bot.formatting import editorial_caption
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.ops.actions_runtime import _busy_state, restored, validate_ready
from relocant_media.ops.correction_delivery import (
    CHANNEL_USERNAME,
    CorrectionReview,
    _digest,
    _get_html,
    _upload_audit,
    _validate_plan_input,
    _validate_reviews,
    visible_post,
)
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.ops.ssh_state_client import SSHStateClient, _state_digest
from relocant_media.services.channel_delivery import APPROVED_CHANNELS_BY_CITY
from relocant_media.services.published_text import effective_published_text, unique_object
from relocant_media.storage.models import EditorialRun, Post, Source

_SHA = r"^[0-9a-f]{64}$"
_LEDGER = "exact_text_corrections"
_MAX_TARGETS = 256


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_text(html: str) -> str:
    """Один распознанный article/main; меню и соседние карточки не доказательства."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, noscript, nav, footer, aside, form"):
        tag.decompose()
    wrappers = soup.select("article.article-wrapper")
    if wrappers:
        if len(wrappers) != 1:
            raise ValueError("Основная статья отсутствует или неоднозначна")
        root = wrappers[0]
        blocks = root.select(":scope > p, :scope > .article-content-wrapper > .rich-text-block")
        if not root.select(":scope > .article-content-wrapper > .rich-text-block"):
            raise ValueError("Полный текст основной статьи отсутствует")
        headings = soup.select("h1")
        if len(headings) != 1:
            raise ValueError("Заголовок основной статьи отсутствует или неоднозначен")
        body = " ".join(block.get_text(" ", strip=True) for block in blocks)
        # Тот же заключительный разбор встроенной разметки, что и в collector.
        body = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
        return " ".join((headings[0].get_text(" ", strip=True) + " " + body).split())
    articles = soup.select("article")
    roots = articles or soup.select("main")
    if len(roots) != 1:
        raise ValueError("Нужен один распознанный блок статьи")
    root = roots[0]
    for tag in root.select("script, style, noscript, nav, header, footer, aside, form"):
        tag.decompose()
    result = " ".join(root.get_text(" ", strip=True).split())
    if len(result) < 10:
        raise ValueError("В статье нет достаточного текста")
    return result


class DocumentProof(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2048)
    document_sha256: str = Field(pattern=_SHA)
    live_text_sha256: str = Field(pattern=_SHA)
    quotes: tuple[str, ...] = Field(min_length=1, max_length=4)


class ExactTextReview(CorrectionReview):
    scope: Literal["remove_unsupported_claims_only"]
    factual_support: Literal["supported"]
    no_new_sensitive_claims: Literal[True]
    reason: str = Field(min_length=12, max_length=1000)

    @field_validator("no_new_sensitive_claims", mode="before")
    @classmethod
    def exact_scope_attestation(cls, value: object) -> object:
        if type(value) is not bool or value is not True:
            raise ValueError("Требуется явное подтверждение области исправления")
        return value


class ExactTextCorrectionPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[4]
    city: Literal["belgrade"]
    operation: Literal["remove_unsupported_claims"]
    raw_item_id: int = Field(gt=0)
    post_id: int = Field(gt=0)
    message_id: int = Field(gt=0)
    old_package_sha256: str = Field(pattern=_SHA)
    old_title_sha256: str = Field(pattern=_SHA)
    old_body_sha256: str = Field(pattern=_SHA)
    old_text_sha256: str = Field(pattern=_SHA)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=6000)
    source_url: str = Field(max_length=2048)
    removed_fragments: tuple[str, ...] = Field(min_length=1, max_length=8)
    documents: tuple[DocumentProof, ...] = Field(min_length=1, max_length=8)
    reviews: tuple[ExactTextReview, ExactTextReview]

    @model_validator(mode="after")
    def bounded_scope(self) -> Self:
        if (not is_public_https_url(self.source_url)
                or len({(doc.source_id, doc.url) for doc in self.documents}) != len(self.documents)
                or any(not is_public_https_url(doc.url) for doc in self.documents)
                or len(set(self.removed_fragments)) != len(self.removed_fragments)
                or any(not 3 <= len(fragment) <= 500 for fragment in self.removed_fragments)
                or any(fragment in self.title + "\n\n" + self.body
                       for fragment in self.removed_fragments)
                or re.search(r"https?://", self.title + self.body)
                or any(not 10 <= len(quote) <= 1000 or quote != " ".join(quote.split())
                       for doc in self.documents for quote in doc.quotes)):
            raise ValueError("Нужен ограниченный план с точными публичными доказательствами")
        return self


def prepared_exact_edit(session: Session, plan: ExactTextCorrectionPlan) -> dict[str, Any]:
    pair = session.execute(select(Post, EditorialRun).join(
        EditorialRun, EditorialRun.post_id == Post.id).where(
            EditorialRun.raw_item_id == plan.raw_item_id, Post.id == plan.post_id,
    )).one_or_none()
    if pair is None:
        raise ValueError("Нет точного исходного опубликованного материала")
    post, run = pair
    if (post.city != plan.city or post.telegram_message_id != plan.message_id
            or post.published_at is None or post.status != "ready"
            or run.delivery_status != "sent" or run.staging_message_id is not None
            or any(key in run.outcome for key in ("_media_delivery", "_rich_delivery"))
            or text_sha256(post.title) != plan.old_title_sha256
            or text_sha256(post.body) != plan.old_body_sha256):
        raise ValueError("Квитанция, исходный текст или тип сообщения изменились")
    package = EditorialPackage.model_validate_json(canonical_json(run.outcome.get("_package")))
    if (package.version != 2 or package.city != plan.city
            or package.raw_item_id != plan.raw_item_id or package.event is None
            or post.category != package.outcome.ranking.category
            or _digest(package.model_dump(mode="json")) != plan.old_package_sha256
            or run.outcome.get("_package_sha256") != plan.old_package_sha256
            or (post.title, post.body) != (package.outcome.draft.title, package.outcome.draft.body)
            or package.presentation is None or package.presentation.native_map is not None
            or package.presentation.source_map is not None
            or package.presentation.illustration.get("illustration_mode") != "source_preview"
            or package.presentation.illustration.get("asset_refs")
            or package.presentation.illustration.get("source_url") != plan.source_url
            or {review.role for review in package.reviews} != {"facts", "utility"}
            or len({review.reviewer_id.strip().casefold() for review in package.reviews}) != 2
            or any(review.context_sha256 != context_sha256(package)
                   or review.draft_sha256 != draft_sha256(package.outcome.draft)
                   or review.verdict != "approve" or review.flags or review.score < 75
                   or not review.live_source_checked for review in package.reviews)):
        raise ValueError("Исходный пакет и его проверки не совпадают с отправленным текстом")
    old = f"{post.title}\n\n{post.body}"
    if any(fragment not in old for fragment in plan.removed_fragments):
        raise ValueError("Удаляемое утверждение отсутствует в исходном тексте")
    documents = {(doc.source_id, doc.url): doc for doc in package.event.documents}
    if set(documents) != {(proof.source_id, proof.url) for proof in plan.documents}:
        raise ValueError("План должен связать весь сохранённый комплект источников")
    for proof in plan.documents:
        source = session.get(Source, proof.source_id)
        document = documents[(proof.source_id, proof.url)]
        if (source is None or not source.enabled or source.city != plan.city
                or (source.ownership, source.high_risk, source.auto_publish)
                != (document.ownership, document.high_risk, document.auto_publish)
                or _digest(document.model_dump(mode="json")) != proof.document_sha256
                or any(quote not in " ".join(document.body.split()) for quote in proof.quotes)):
            raise ValueError("Источник или точные доказательства исходного пакета изменились")
    if plan.source_url not in {doc.url for doc in package.event.documents}:
        raise ValueError("Ссылка источника не принадлежит исходному пакету")
    old_text, _ = editorial_caption(old + "\n\nИсточник: " + plan.source_url)
    new_text, entities = editorial_caption(
        plan.title + "\n\n" + plan.body + "\n\nИсточник: " + plan.source_url)
    if (text_sha256(old_text) != plan.old_text_sha256 or old_text == new_text
            or len(new_text.encode("utf-16-le")) // 2 > 4096):
        raise ValueError("Исходный отпечаток или размер исправления недопустим")
    return {"old_text": old_text, "payload": {
        "chat_id": APPROVED_CHANNELS_BY_CITY[plan.city], "message_id": plan.message_id,
        "text": new_text, "entities": list(entities), "link_preview_options": {
            "url": plan.source_url, "prefer_large_media": True, "show_above_text": True}}}


def _public(client: httpx.Client, plan: ExactTextCorrectionPlan) -> tuple[str, str]:
    return visible_post(_get_html(client,
        f"https://t.me/{CHANNEL_USERNAME}/{plan.message_id}?embed=1"), plan.message_id)


def _confirmed(result: Any, payload: dict[str, Any]) -> bool:
    return bool(isinstance(result, dict) and type(result.get("message_id")) is int
        and result["message_id"] == payload["message_id"]
        and isinstance(result.get("chat"), dict) and type(result["chat"].get("id")) is int
        and result["chat"]["id"] == payload["chat_id"] and result["chat"].get("type") == "channel"
        and result.get("text") == payload["text"]
        and sorted(map(canonical_json, result.get("entities", [])))
        == sorted(map(canonical_json, payload["entities"]))
        and not any(key in result for key in
                    ("photo", "video", "document", "caption", "reply_markup")))


def _restore_edit(storage: SSHStateClient, state: dict[str, Any], work: Path,
                  plan: ExactTextCorrectionPlan) -> tuple[Path, dict[str, Any]]:
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    database = restored(storage, state["snapshot"], work)
    uri = database.resolve().as_uri() + "?mode=ro"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    try:
        with Session(engine) as session:
            session.execute(text("PRAGMA query_only=ON"))
            return database, prepared_exact_edit(session, plan)
    finally:
        engine.dispose()


def _check_sources(client: httpx.Client, plan: ExactTextCorrectionPlan) -> None:
    for proof in plan.documents:
        live = source_text(_get_html(client, proof.url))
        if (text_sha256(live) != proof.live_text_sha256
                or any(quote not in live for quote in proof.quotes)):
            raise ValueError("Живой источник изменил проверенный текст")


@dataclass(frozen=True)
class _Commit:
    ledger: dict[str, Any]
    key: str
    target: dict[str, Any]
    intent: dict[str, Any]
    intent_sha: str
    busy_sha: str
    recovery: dict[str, Any]
    checkpoint_sha: str


def _claim(storage: SSHStateClient, state: dict[str, Any], state_sha: str,
           ledger: dict[str, Any], key: str, target: dict[str, Any], intent: dict[str, Any],
           intent_sha: str, work: Path) -> _Commit:
    busy = _busy_state(state, os.environ)
    busy[_LEDGER] = {**ledger, key: target}
    # Контрольная точка не разрешает повтор внешнего edit.
    recovery = {**busy, "status": "ready", "generation": busy["attempt"]}
    _state_digest({**recovery, _LEDGER: {**ledger, key: {**target, "receipt_sha256": "0" * 64}}})
    checkpoint_sha = storage.checkpoint(recovery, _state_digest(busy), work / "checkpoint.json")
    print(json.dumps({"correction_intent_sha256": intent_sha,
                      "recovery_checkpoint_sha256": checkpoint_sha}), flush=True)
    busy_sha = storage.write(busy, state_sha)
    if storage.read() != (busy, busy_sha):
        raise ValueError("Захват исправления не подтверждён каноническим чтением")
    return _Commit(ledger, key, target, intent, intent_sha, busy_sha, recovery, checkpoint_sha)


def _read_record(storage: SSHStateClient, sha: Any, *, maximum: int = 32 * 1024) -> dict[str, Any]:
    if not isinstance(sha, str) or not re.fullmatch(_SHA, sha):
        raise ValueError("Нужен сохранённый SHA записи исправления")
    content = storage.download(sha)
    if len(content) > maximum or hashlib.sha256(content).hexdigest() != sha:
        raise ValueError("Хеш или размер сохранённой записи не совпал")
    record = json.loads(content, object_pairs_hook=unique_object)
    if not isinstance(record, dict):
        raise ValueError("Повреждена сохранённая запись исправления")
    return record


def _api_proof(storage: SSHStateClient, previous: dict[str, Any], plan: ExactTextCorrectionPlan,
               plan_sha: str, edit: dict[str, Any], key: str,
               now: datetime) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Проверить старое API-подтверждение, не создавать новое и не менять его смысл."""
    receipt = _read_record(storage, previous.get("receipt_sha256"))
    intent = _read_record(storage, previous.get("intent_sha256"))
    if receipt.get("api_confirmed") is not True:
        return None
    payload = edit["payload"]
    expected = {"version": 4, "operation": "editMessageText", "plan_sha256": plan_sha,
        "old_package_sha256": plan.old_package_sha256, "old_text_sha256": plan.old_text_sha256,
        "new_text_sha256": text_sha256(payload["text"]), "payload_sha256": _digest(payload),
        "chat_id": payload["chat_id"], "message_id": plan.message_id}
    if (type(previous.get("attempts")) is not int or previous["attempts"] != 1
            or previous.get("status") != "uncertain"
            or previous.get("old_text_sha256") != expected["old_text_sha256"]
            or previous.get("new_text_sha256") != expected["new_text_sha256"]
            or receipt.get("intent_sha256") != previous["intent_sha256"]
            or receipt.get("status") != "uncertain" or receipt.get("public_confirmed") is not False
            or "error_type" in receipt
            or any(type(intent.get(field)) is not type(value) or intent.get(field) != value
                   for field, value in expected.items())
            or any(type(receipt.get(field)) is not type(value) or receipt.get(field) != value
                   for field, value in intent.items())
            or not re.fullmatch(_SHA, str(intent.get("snapshot_sha256", "")))
            or not re.fullmatch(_SHA, str(intent.get("canonical_state_sha256", "")))):
        raise ValueError("Сохранённое API-подтверждение не связано с точным планом и получателем")
    checkpoint = _read_record(storage, receipt.get("checkpoint_sha256"), maximum=512 * 1024)
    checkpoint_state = checkpoint.get("state", {})
    validate_ready(checkpoint_state)
    checkpoint_target = checkpoint_state.get(_LEDGER, {}).get(key)
    if (type(checkpoint.get("version")) is not int or checkpoint["version"] != 1
            or not re.fullmatch(_SHA, str(checkpoint.get("expected_busy_sha256", "")))
            or checkpoint_state["snapshot"]["sha256"] != intent["snapshot_sha256"]
            or checkpoint_state.get("generation") != checkpoint_state.get("attempt")
            or not isinstance(checkpoint_target, dict)
            or set(previous) != set(checkpoint_target) | {"receipt_sha256"}
            or any(type(previous.get(field)) is not type(value) or previous.get(field) != value
                   for field, value in checkpoint_target.items())
            or checkpoint_target.get("intent_sha256") != previous["intent_sha256"]):
        raise ValueError("Исходная контрольная точка не подтверждает эту операцию")
    checked_at = datetime.fromisoformat(intent["checked_at"])
    _validate_reviews(plan, checked_at)
    if checked_at > now or previous.get("recorded_at") != intent["checked_at"]:
        raise ValueError("Время исходного подтверждения не согласовано")
    return intent, receipt


def _reconcile(storage: SSHStateClient, content: bytes, plan: ExactTextCorrectionPlan,
               plan_sha: str, work: Path, client: httpx.Client, now: datetime,
               state: dict[str, Any], state_sha: str, ledger: dict[str, Any], key: str,
               previous: dict[str, Any]) -> dict[str, Any]:
    """Только GET и локальная фиксация; token и право внешнего edit сюда не передаются."""
    unresolved = {"status": "uncertain", "receipt_sha256": previous.get("receipt_sha256")}
    if previous.get("status") != "uncertain" or not previous.get("receipt_sha256"):
        return unresolved
    database, edit = _restore_edit(storage, state, work, plan)
    proof = _api_proof(storage, previous, plan, plan_sha, edit, key, now)
    if proof is None:
        return unresolved
    intent, old_receipt = proof
    _check_sources(client, plan)
    if _public(client, plan) != (edit["payload"]["text"], plan.source_url):
        return unresolved
    if storage.read() != (state, state_sha):
        raise ValueError("Каноническое состояние изменилось при сверке")
    commit = _claim(storage, state, state_sha, ledger, key, previous,
                    intent, previous["intent_sha256"], work)
    receipt = {**old_receipt, "status": "corrected", "public_confirmed": True,
        "public_text_sha256": text_sha256(edit["payload"]["text"]),
        "previous_receipt_sha256": previous["receipt_sha256"], "previous_receipt": old_receipt,
        "reconciliation_kind": "late_public_readback",
        "reconciled_at": now.isoformat(), "reconciliation_checkpoint_sha256": commit.checkpoint_sha,
        "reconciliation_state_sha256": state_sha,
        "reconciliation_snapshot_sha256": state["snapshot"]["sha256"]}
    return _finish(storage, content, plan, plan_sha, work, database, commit, receipt)


def execute_exact_text_edit(
    storage: SSHStateClient, content: bytes, plan_sha: str, work: Path,
    client: httpx.Client, token: str, now: datetime,
) -> dict[str, Any]:
    _validate_plan_input(content, plan_sha, now)
    json.loads(content, object_pairs_hook=unique_object)
    plan = ExactTextCorrectionPlan.model_validate_json(content)
    state, state_sha = storage.read()
    validate_ready(state)
    key = f"{APPROVED_CHANNELS_BY_CITY[plan.city]}:{plan.message_id}"
    ledger = state.get(_LEDGER, {})
    if not isinstance(ledger, dict) or len(ledger) > _MAX_TARGETS:
        raise ValueError("Повреждён или заполнен журнал исправлений")
    previous = ledger.get(key)
    if previous is not None:
        if not isinstance(previous, dict) or previous.get("plan_sha256") != plan_sha:
            raise ValueError("Для этого сообщения уже сохранено другое намерение исправления")
        if previous.get("status") == "corrected":
            current, source = _public(client, plan)
            if (text_sha256(current) != previous.get("new_text_sha256")
                    or source != plan.source_url):
                raise ValueError("Подтверждённое исправление изменилось публично")
            return {"status": "already_corrected", "receipt_sha256": previous["receipt_sha256"]}
        # SAME PLAN может только сверить известный APIack; token сюда не передаётся.
        return _reconcile(storage, content, plan, plan_sha, work, client, now,
                          state, state_sha, ledger, key, previous)
    if len(ledger) >= _MAX_TARGETS:
        raise ValueError("Журнал исправлений заполнен; автоматического удаления истории нет")
    _validate_reviews(plan, now)
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
        raise ValueError("Некорректный токен Bot API")
    database, edit = _restore_edit(storage, state, work, plan)
    _check_sources(client, plan)
    if _public(client, plan) != (edit["old_text"], plan.source_url):
        raise ValueError("Публичный текст не совпадает с точным исходным сообщением")
    fresh, fresh_sha = storage.read()
    validate_ready(fresh)
    if (fresh, fresh_sha) != (state, state_sha):
        raise ValueError("Каноническое состояние изменилось при проверке")
    payload = edit["payload"]
    intent = {"version": 4, "operation": "editMessageText", "plan_sha256": plan_sha,
        "canonical_state_sha256": state_sha, "snapshot_sha256": state["snapshot"]["sha256"],
        "old_package_sha256": plan.old_package_sha256, "old_text_sha256": plan.old_text_sha256,
        "new_text_sha256": text_sha256(payload["text"]), "payload_sha256": _digest(payload),
        "chat_id": payload["chat_id"], "message_id": plan.message_id,
        "checked_at": now.isoformat()}
    intent_sha = _upload_audit(storage, work, "intent", intent)
    target = {"plan_sha256": plan_sha, "intent_sha256": intent_sha, "status": "uncertain",
        "old_text_sha256": plan.old_text_sha256, "new_text_sha256": intent["new_text_sha256"],
        "attempts": 1, "recorded_at": now.isoformat()}
    commit = _claim(storage, state, state_sha, ledger, key, target, intent, intent_sha, work)
    receipt: dict[str, Any] = {**intent, "intent_sha256": intent_sha,
        "checkpoint_sha256": commit.checkpoint_sha, "status": "uncertain", "api_confirmed": False,
        "public_confirmed": False}
    try:
        response = client.post(f"https://api.telegram.org/bot{token}/editMessageText",
                               json=payload, timeout=30, follow_redirects=False)
        answer = response.json()
        receipt["api_confirmed"] = (response.is_success and isinstance(answer, dict)
            and answer.get("ok") is True and _confirmed(answer.get("result"), payload))
    except Exception as error:
        receipt["error_type"] = type(error).__name__
    try:
        current, source = _public(client, plan)
        receipt["public_text_sha256"] = text_sha256(current)
        receipt["public_confirmed"] = (current, source) == (payload["text"], plan.source_url)
    except Exception as error:
        receipt["public_error_type"] = type(error).__name__
    if receipt["api_confirmed"] and receipt["public_confirmed"]:
        receipt["status"] = "corrected"
    return _finish(storage, content, plan, plan_sha, work, database, commit, receipt)


def _finish(storage: SSHStateClient, content: bytes, plan: ExactTextCorrectionPlan,
            plan_sha: str, work: Path, database: Path, commit: _Commit,
            receipt: dict[str, Any]) -> dict[str, Any]:
    ledger, key, target = commit.ledger, commit.key, commit.target
    intent, intent_sha = commit.intent, commit.intent_sha
    recovery, busy_sha = commit.recovery, commit.busy_sha
    receipt_sha = _upload_audit(storage, work, "receipt", receipt)
    final_target = {**target, "status": receipt["status"], "receipt_sha256": receipt_sha}
    ready = {**recovery, _LEDGER: {**ledger, key: final_target}}
    if receipt["status"] == "corrected":
        overlay = {"version": 4, "status": "corrected", "plan_json": content.decode("utf-8"),
            "plan_sha256": plan_sha, "intent_sha256": intent_sha, "receipt_sha256": receipt_sha,
            "intent": intent, "receipt": receipt}
        writer = create_engine(f"sqlite:///{database.as_posix()}")
        try:
            with Session(writer) as session:
                # Повторно проверяем локальный исходный snapshot перед append-only overlay.
                prepared_exact_edit(session, plan)
                run = session.get(EditorialRun, plan.raw_item_id)
                assert run is not None
                if "_published_correction" in run.outcome:
                    raise ValueError("История исправления уже существует")
                run.outcome = {**run.outcome, "_published_correction": overlay}
                post = session.get(Post, plan.post_id)
                assert post is not None
                if not effective_published_text(post, run).corrected:
                    raise ValueError("Связи подтверждённого исправления не проходят проверку")
                session.commit()
        finally:
            writer.dispose()
        manifest = create_snapshot(database, work / "corrected-snapshot")
        snapshot_file = work / "corrected-snapshot" / "editorial.sqlite3"
        snapshot_sha = storage.upload(snapshot_file, "snapshot")
        if (snapshot_sha != manifest["sha256"]
                or hashlib.sha256(storage.download(snapshot_sha)).hexdigest() != snapshot_sha):
            raise ValueError("Readback исправленного снимка не подтверждён")
        ready["snapshot"] = {**manifest, "drive_file_id": snapshot_sha}
    final_checkpoint = storage.checkpoint(ready, busy_sha, work / "final-checkpoint.json")
    print(json.dumps({"recovery_checkpoint_sha256": final_checkpoint}), flush=True)
    final_sha = storage.write(ready, busy_sha)
    if storage.read() != (ready, final_sha):
        raise ValueError("Финальная квитанция исправления требует сверки")
    return {"status": receipt["status"], "intent_sha256": intent_sha,
            "receipt_sha256": receipt_sha}
