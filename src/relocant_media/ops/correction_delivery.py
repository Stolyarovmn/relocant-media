"""Ручные исправления опубликованного текста или подписи сводки."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json, context_sha256, draft_sha256
from relocant_media.bot.api import map_reply_markup
from relocant_media.bot.formatting import compact_source_link, formatted_text_units
from relocant_media.ops.actions_runtime import REPO, restored, validate_ready
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient
from relocant_media.services.channel_delivery import APPROVED_CHANNELS_BY_CITY
from relocant_media.storage.models import EditorialRun, Post

MAX_PLAN_BYTES = 32 * 1024
MAX_HTML_BYTES = 2_000_000
CHANNEL_USERNAME = "eduvbelgrad"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class CorrectionReview(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    reviewer_id: str = Field(min_length=1, max_length=100)
    role: Literal["facts", "utility"]
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at: AwareDatetime
    verdict: Literal["approve", "hold", "reject"]
    score: int = Field(ge=0, le=100)
    flags: tuple[str, ...]
    live_source_checked: bool


class CorrectionPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[1]
    city: Literal["belgrade"]
    raw_item_id: int = Field(gt=0)
    message_id: int = Field(gt=0)
    old_fragment: str = Field(min_length=1, max_length=500)
    new_fragment: str = Field(min_length=1, max_length=500)
    evidence_url: str = Field(min_length=1, max_length=500)
    evidence_text: str = Field(min_length=20, max_length=2000)
    reviews: tuple[CorrectionReview, CorrectionReview]

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Версия должна быть целым числом")
        return value

    @model_validator(mode="after")
    def narrow_change(self) -> Self:
        if any(character in self.old_fragment + self.new_fragment for character in "\r\n"):
            raise ValueError("Исправление ограничено одной строкой")
        old = re.fullmatch(r"([^0-9]*)([0-9]{1,6})([^0-9]*)", self.old_fragment)
        new = re.fullmatch(r"([^0-9]*)([0-9]{1,6})([^0-9]*)", self.new_fragment)
        if (old is None or new is None or old[1] != new[1] or old[3] != new[3]
                or old[2] == new[2] or new[2] not in re.findall(r"\d+", self.evidence_text)):
            raise ValueError("Разрешена только замена одного подтверждённого номера")
        url = urlsplit(self.evidence_url)
        if (url.scheme != "https" or url.netloc not in {"bgprevoz.rs", "www.bgprevoz.rs"}
                or not re.fullmatch(r"/linije/aktuelne-izmene/[0-9]+", url.path)
                or url.query or url.fragment):
            raise ValueError("Нужен разрешённый первоисточник Секретариата")
        return self


class DigestCorrectionPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[2]
    city: Literal["belgrade"]
    message_id: int = Field(gt=0)
    slot: str = Field(pattern=r"^belgrade:[0-9]{4}-[0-9]{2}-[0-9]{2}:(morning|evening)$")
    old_caption_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    caption_html: str = Field(min_length=1, max_length=6000)
    map_url: str
    reviews: tuple[CorrectionReview, CorrectionReview]

    @model_validator(mode="after")
    def valid_caption(self) -> Self:
        if not 0 < formatted_text_units(self.caption_html) <= 1024:
            raise ValueError("Подпись сводки превышает предел Telegram")
        map_reply_markup(self.map_url)
        return self


def _validate_plan_input(content: bytes, expected_sha: str, now: datetime) -> None:
    if (not _SHA.fullmatch(expected_sha) or len(content) > MAX_PLAN_BYTES
            or hashlib.sha256(content).hexdigest() != expected_sha):
        raise ValueError("Хеш или размер плана не соответствует запросу")
    if now.tzinfo is None:
        raise ValueError("Время проверки должно содержать часовой пояс")


def _validate_reviews(plan: CorrectionPlan | DigestCorrectionPlan, now: datetime) -> None:
    context = _digest(plan.model_dump(mode="json", exclude={"reviews"}))
    identities = {review.reviewer_id.strip().casefold() for review in plan.reviews}
    if (len(identities) != 2 or "" in identities
            or {review.role for review in plan.reviews} != {"facts", "utility"}
            or any(review.verdict != "approve" or review.score < 75 or review.flags
                   or not review.live_source_checked or review.context_sha256 != context
                   or not timedelta(0) <= now - review.reviewed_at <= timedelta(hours=2)
                   for review in plan.reviews)):
        raise ValueError("Нужны два свежих независимых одобрения точного плана")


def validate_plan(content: bytes, expected_sha: str, *, now: datetime) -> CorrectionPlan:
    _validate_plan_input(content, expected_sha, now)
    plan = CorrectionPlan.model_validate_json(content)
    _validate_reviews(plan, now)
    return plan


def prepared_edit(session: Session, plan: CorrectionPlan) -> dict[str, Any]:
    row = session.execute(select(Post, EditorialRun).join(
        EditorialRun, EditorialRun.post_id == Post.id).where(
            EditorialRun.raw_item_id == plan.raw_item_id)).one_or_none()
    if row is None:
        raise ValueError("Опубликованный материал не найден")
    post, run = row
    if (post.city != plan.city or post.telegram_message_id != plan.message_id
            or run.delivery_status != "sent" or run.staging_message_id is not None
            or post.published_at is None or post.status != "ready"
            or "_media_delivery" in run.outcome):
        raise ValueError("Квитанция или тип опубликованного сообщения не соответствует плану")
    package = EditorialPackage.model_validate_json(canonical_json(run.outcome.get("_package")))
    if (package.version != 2 or package.city != plan.city or package.raw_item_id != plan.raw_item_id
            or run.outcome.get("_package_sha256") != _digest(package.model_dump(mode="json"))
            or (post.title, post.body) != (package.outcome.draft.title, package.outcome.draft.body)
            or package.event is None or package.presentation is None
            or package.presentation.native_map is not None
            or package.presentation.source_map is not None
            or package.presentation.illustration.get("illustration_mode") != "source_preview"
            or package.presentation.illustration.get("asset_refs")):
        raise ValueError("Исходный проверенный текст изменён или содержит отдельные медиа")
    if ({review.role for review in package.reviews} != {"facts", "utility"}
            or len({review.reviewer_id.strip().casefold() for review in package.reviews}) != 2
            or any(review.context_sha256 != context_sha256(package)
                   or review.draft_sha256 != draft_sha256(package.outcome.draft)
                   or review.verdict != "approve" or review.flags or review.score < 75
                   or not review.live_source_checked for review in package.reviews)):
        raise ValueError("Исходные одобрения не соответствуют опубликованному пакету")
    lines = post.body.splitlines()
    if lines.count(plan.old_fragment) != 1 or plan.new_fragment in lines:
        raise ValueError("Исходная строка должна встречаться в сохранённом тексте ровно один раз")
    source_url = next((document.url for document in package.event.documents
                       if document.ownership.strip().casefold() == "official"),
                      package.evidence.url)
    if package.presentation.illustration.get("source_url") != source_url:
        raise ValueError("Исходная ссылка не соответствует проверенному оформлению")
    body = "".join(plan.new_fragment + line[len(plan.old_fragment):]
                   if line.rstrip("\r\n") == plan.old_fragment else line
                   for line in post.body.splitlines(keepends=True))
    old_text, _ = compact_source_link(f"{post.title}\n\n{post.body}\n\nИсточник: {source_url}")
    new_text, entities = compact_source_link(
        f"{post.title}\n\n{body}\n\nИсточник: {plan.evidence_url}")
    if len(new_text.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Исправленный текст превышает лимит Telegram")
    return {"old_text": old_text, "old_source_url": source_url,
            "package_sha256": run.outcome["_package_sha256"],
            "payload": {"chat_id": APPROVED_CHANNELS_BY_CITY[plan.city],
                        "message_id": plan.message_id, "text": new_text,
                        "entities": list(entities), "link_preview_options": {
                            "url": plan.evidence_url, "prefer_large_media": True,
                            "show_above_text": True}}}


def _get_html(client: httpx.Client, url: str) -> str:
    content = bytearray()
    with client.stream("GET", url, follow_redirects=False, timeout=25) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > MAX_HTML_BYTES:
                raise ValueError("Публичная страница превышает допустимый размер")
    return bytes(content).decode("utf-8", errors="strict")


def visible_post(html: str, message_id: int) -> tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select(f'.tgme_widget_message[data-post="{CHANNEL_USERNAME}/{message_id}"]')
    if len(nodes) != 1:
        raise ValueError("Публичное сообщение недоступно или неоднозначно")
    node = nodes[0]
    if node.select_one(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player, "
                       ".tgme_widget_message_document, .tgme_widget_message_grouped_wrap"):
        raise ValueError("Исправлять можно только текстовое сообщение")
    content = node.select_one(".tgme_widget_message_text")
    if content is None:
        raise ValueError("Публичный текст отсутствует")
    links = [anchor for anchor in content.select("a[href]")
             if anchor.get_text() == "Источник"]
    if len(links) != 1:
        raise ValueError("Публичная ссылка на источник неоднозначна")
    source_url = str(links[0]["href"])
    for br in content.find_all("br"):
        br.replace_with("\n")
    return content.get_text().strip(), source_url


def _upload_audit(storage: SSHStateClient, work: Path, name: str, record: dict[str, Any]) -> str:
    path = work / f"{name}.json"
    with path.open("xb") as stream:
        stream.write(canonical_json(record).encode("utf-8"))
    return storage.upload(path, "correction-audit")


def _confirm_result(result: Any, payload: dict[str, Any]) -> None:
    if (not isinstance(result, dict) or type(result.get("message_id")) is not int
            or result["message_id"] != payload["message_id"]
            or not isinstance(result.get("chat"), dict)
            or type(result["chat"].get("id")) is not int
            or result["chat"]["id"] != payload["chat_id"]
            or result["chat"].get("type") != "channel" or result.get("text") != payload["text"]
            or result.get("entities") != payload["entities"]
            or any(key in result for key in ("photo", "video", "document", "caption"))):
        raise ValueError("Bot API не подтвердил точный исправленный текст и получателя")


def _caption_signature(html: str) -> dict[str, Any]:
    node = BeautifulSoup(html, "html.parser")
    for br in node.find_all("br"):
        br.replace_with("\n")
    for quote in node.select("blockquote"):
        quote.insert_before("\n")
        quote.insert_after("\n")
    return {
        "text": " ".join(node.get_text().split()),
        "links": [str(link["href"]) for link in node.select("a[href]")],
        "quotes": len(node.select("blockquote[expandable]")),
    }


def _edit_digest(
    storage: SSHStateClient, plan: DigestCorrectionPlan, plan_sha: str, work: Path,
    client: httpx.Client, token: str, checked_at: datetime,
) -> dict[str, Any]:
    state, _ = storage.read()
    receipt = state.get("digest_receipts", {}).get(plan.slot, {})
    if receipt.get("state") != "sent" or receipt.get("message_id") != plan.message_id:
        raise ValueError("Отправленная сводка не найдена в квитанциях")
    public_url = f"https://t.me/{CHANNEL_USERNAME}/{plan.message_id}?embed=1"
    soup = BeautifulSoup(_get_html(client, public_url), "html.parser")
    node = soup.select_one(f'[data-post="{CHANNEL_USERNAME}/{plan.message_id}"]')
    if node is None or node.select_one(".tgme_widget_message_photo_wrap") is None:
        raise ValueError("Ожидалась существующая сводка с фотографией")
    caption = node.select_one(".tgme_widget_message_text")
    if caption is None:
        raise ValueError("У сводки отсутствует подпись")
    expected = _caption_signature(plan.caption_html)
    visible = _caption_signature(caption.decode_contents())
    if visible != expected and _digest(visible) != plan.old_caption_sha256:
        raise ValueError("Опубликованная подпись изменилась")
    chat_id = APPROVED_CHANNELS_BY_CITY[plan.city]
    payload = {"chat_id": chat_id, "message_id": plan.message_id,
               "caption": plan.caption_html, "parse_mode": "HTML",
               "reply_markup": map_reply_markup(plan.map_url)}
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    intent_sha = _upload_audit(storage, work, "intent", {
        "version": 2, "operation": "editMessageCaption", "plan_sha256": plan_sha,
        "edit_payload_sha256": _digest(payload), "message_id": plan.message_id,
        "chat_id": chat_id, "checked_at": checked_at.isoformat(),
    })
    print(json.dumps({"correction_intent_sha256": intent_sha}), flush=True)
    record: dict[str, Any] = {
        "version": 2, "intent_sha256": intent_sha, "message_id": plan.message_id,
        "chat_id": chat_id, "recorded_at": datetime.now(UTC).isoformat(),
    }
    try:
        if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
            raise ValueError("Некорректный токен Bot API")
        response = client.post(f"https://api.telegram.org/bot{token}/editMessageCaption",
                               json=payload, timeout=30, follow_redirects=False)
        body = response.json()
        result = body.get("result", {})
        if (response.status_code == 400 and visible == expected
                and body.get("description", "").startswith("Bad Request: message is not modified")):
            record["status"] = "already_corrected"
        elif (body.get("ok") is True and result.get("message_id") == plan.message_id
              and result.get("chat", {}).get("id") == chat_id and result.get("photo")
              and " ".join(result.get("caption", "").split()) == expected["text"]
              and [entity["url"] for entity in result.get("caption_entities", [])
                   if entity["type"] == "text_link"] == expected["links"]
              and sum(entity["type"] == "expandable_blockquote"
                      for entity in result.get("caption_entities", [])) == expected["quotes"]
              and result.get("reply_markup") == payload["reply_markup"]):
            record.update(status="corrected", expandable_quotes=expected["quotes"],
                          map_url=plan.map_url)
        else:
            record.update(status="uncertain", telegram_status=response.status_code)
    except Exception as error:
        record.update(status="uncertain", error_type=type(error).__name__)
    receipt_sha = _upload_audit(storage, work, "receipt", record)
    return {**record, "receipt_sha256": receipt_sha}


def execute(
    storage: SSHStateClient, plan_sha: str, work: Path, client: httpx.Client, *,
    token: str, now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = now or datetime.now(UTC)
    if not _SHA.fullmatch(plan_sha):
        raise ValueError("Нужен SHA256 приватного плана")
    content = storage.download(plan_sha)
    _validate_plan_input(content, plan_sha, checked_at)
    if json.loads(content).get("version") == 2:
        digest_plan = DigestCorrectionPlan.model_validate_json(content)
        _validate_reviews(digest_plan, checked_at)
        return _edit_digest(storage, digest_plan, plan_sha, work, client, token, checked_at)
    plan = validate_plan(content, plan_sha, now=checked_at)
    state, state_sha = storage.read()
    validate_ready(state)
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    database = restored(storage, state["snapshot"], work)
    engine = create_engine(f"sqlite:///file:{database.as_posix()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            session.execute(text("PRAGMA query_only=ON"))
            edit = prepared_edit(session, plan)
    finally:
        engine.dispose()
    source = BeautifulSoup(_get_html(client, plan.evidence_url), "html.parser")
    for tag in source.select("script, style, noscript"):
        tag.decompose()
    visible_source = " ".join(source.get_text(" ", strip=True).split())
    if " ".join(plan.evidence_text.split()) not in visible_source:
        raise ValueError("Живой первоисточник не подтверждает точную цитату плана")
    current_text, current_url = visible_post(_get_html(
        client, f"https://t.me/{CHANNEL_USERNAME}/{plan.message_id}?embed=1"), plan.message_id)
    payload = edit["payload"]
    already = (current_text, current_url) == (payload["text"], plan.evidence_url)
    if not already and (current_text, current_url) != (edit["old_text"], edit["old_source_url"]):
        raise ValueError("Публичный пост не совпадает ни с исходным, ни с исправленным текстом")
    current_state, current_sha = storage.read()
    validate_ready(current_state)
    if current_sha != state_sha or current_state != state:
        raise ValueError("Каноническое состояние изменилось во время проверки")
    intent = {"version": 1, "operation": "editMessageText", "plan_sha256": plan_sha,
              "plan_context_sha256": _digest(plan.model_dump(mode="json", exclude={"reviews"})),
              "canonical_state_sha256": state_sha, "snapshot_sha256": state["snapshot"]["sha256"],
              "package_sha256": edit["package_sha256"], "edit_payload_sha256": _digest(payload),
              "raw_item_id": plan.raw_item_id, "message_id": plan.message_id,
              "chat_id": payload["chat_id"], "checked_at": checked_at.isoformat(),
              "visible_state": "corrected" if already else "original"}
    intent_sha = _upload_audit(storage, work, "intent", intent)
    print(json.dumps({"correction_intent_sha256": intent_sha}), flush=True)
    receipt: dict[str, Any] = {"version": 1, "intent_sha256": intent_sha,
                               "plan_sha256": plan_sha, "message_id": plan.message_id,
                               "chat_id": payload["chat_id"],
                               "edit_payload_sha256": _digest(payload)}
    if already:
        receipt["status"] = "already_corrected"
    else:
        try:
            if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
                raise ValueError("Некорректный токен Bot API")
            response = client.post(f"https://api.telegram.org/bot{token}/editMessageText",
                                   json=payload, timeout=30, follow_redirects=False)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise ValueError("Bot API не подтвердил редактирование")
            _confirm_result(body.get("result"), payload)
            receipt["status"] = "corrected"
        except Exception as error:
            receipt.update(status="uncertain", error_type=type(error).__name__)
    receipt["recorded_at"] = datetime.now(UTC).isoformat()
    receipt_sha = _upload_audit(storage, work, "receipt", receipt)
    return {"status": receipt["status"], "message_id": plan.message_id,
            "intent_sha256": intent_sha, "receipt_sha256": receipt_sha}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-sha", required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    try:
        if (os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("GITHUB_REPOSITORY") != REPO
                or os.environ.get("GITHUB_REF") != "refs/heads/main"):
            raise ValueError("Исправление разрешено только отдельным ручным Actions-запуском main")
        storage = SSHStateClient(SSHConfig.from_environment(os.environ))
        with httpx.Client(trust_env=False, transport=httpx.HTTPTransport(retries=0)) as client:
            result = execute(storage, args.plan_sha, args.work, client,
                             token=os.environ["TELEGRAM_BOT_TOKEN"])
    except Exception as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))
    if result["status"] not in {"corrected", "already_corrected"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
