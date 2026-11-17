"""Проверяемая правка существующей афиши: тот же пост, источник и карта из реестра."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json, context_sha256, draft_sha256
from relocant_media.bot.api import map_reply_markup
from relocant_media.bot.formatting import editorial_caption
from relocant_media.domain.map_links import map_event_url
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.ops.actions_runtime import restored, validate_ready
from relocant_media.ops.correction_delivery import (
    CorrectionReview,
    _digest,
    _get_html,
    _upload_audit,
    _validate_reviews,
    visible_post,
)
from relocant_media.ops.events_export import ReviewedMapEvent, read_events_feed
from relocant_media.ops.ssh_state_client import SSHStateClient
from relocant_media.services.channel_delivery import APPROVED_CHANNELS_BY_CITY
from relocant_media.storage.models import EditorialRun, Post, RawItem

_FEED = "https://stolyarovmn.github.io/relocant-media/map/events.json"


class PageEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(max_length=2048)
    text: str = Field(min_length=10, max_length=1000)


class EventPostCorrectionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[3]
    city: Literal["belgrade"]
    raw_item_id: int = Field(gt=0)
    message_id: int = Field(gt=0)
    old_package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=6000)
    source_url: str
    map_event_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: tuple[PageEvidence, ...] = Field(min_length=2, max_length=4)
    reviews: tuple[CorrectionReview, CorrectionReview]

    @model_validator(mode="after")
    def public_sources(self) -> EventPostCorrectionPlan:
        if (any(not is_public_https_url(url) for url in (
            self.source_url, *(row.url for row in self.evidence),
        )) or len({row.url for row in self.evidence}) != len(self.evidence)):
            raise ValueError("Нужны разные публичные HTTPS-страницы доказательств")
        return self


def reviewed_event(plan: EventPostCorrectionPlan) -> ReviewedMapEvent:
    records = TypeAdapter(tuple[ReviewedMapEvent, ...]).validate_json(
        Path(f"cities/{plan.city}/reviewed-map-events.json").read_bytes(),
    )
    matches = [row for row in records if row.message_id == plan.message_id
               and _digest(row.model_dump(mode="json", exclude_unset=True))
               == plan.map_event_sha256]
    if len(matches) != 1:
        raise ValueError("План не совпадает с проверенной записью карты")
    event = matches[0]
    if event.source_url != plan.source_url or not set(row.url for row in plan.evidence).issubset(
        event.evidence_urls,
    ):
        raise ValueError("Доказательства не совпадают с проверенным реестром")
    return event


def prepared_event_edit(
    session: Session, plan: EventPostCorrectionPlan, event: ReviewedMapEvent,
) -> dict[str, Any]:
    row = session.execute(select(Post, EditorialRun, RawItem).join(
        EditorialRun, EditorialRun.post_id == Post.id).join(
        RawItem, RawItem.id == EditorialRun.raw_item_id).where(
        EditorialRun.raw_item_id == plan.raw_item_id, Post.city == plan.city,
    )).one_or_none()
    if row is None:
        raise ValueError("Опубликованная афиша не найдена")
    post, run, raw = row
    if (post.telegram_message_id != plan.message_id or post.published_at is None
            or post.category not in {"events", "culture"} or post.status != "ready"
            or run.delivery_status != "sent" or run.staging_message_id is not None
            or "_media_delivery" in run.outcome or post.title != event.post_title
            or raw.url != plan.source_url):
        raise ValueError("Квитанция, текст или тип сообщения изменились")
    package = EditorialPackage.model_validate_json(canonical_json(run.outcome.get("_package")))
    if (_digest(package.model_dump(mode="json")) != plan.old_package_sha256
            or run.outcome.get("_package_sha256") != plan.old_package_sha256
            or package.version != 2 or package.raw_item_id != plan.raw_item_id
            or package.city != plan.city or package.presentation is None
            or package.presentation.illustration.get("source_url") != plan.source_url
            or package.presentation.illustration.get("illustration_mode") != "source_preview"
            or (post.title, post.body) != (package.outcome.draft.title, package.outcome.draft.body)
            or {review.role for review in package.reviews} != {"facts", "utility"}
            or len({review.reviewer_id.casefold() for review in package.reviews}) != 2
            or any(review.verdict != "approve" or review.score < 75 or review.flags
                   or not review.live_source_checked
                   or review.context_sha256 != context_sha256(package)
                   or review.draft_sha256 != draft_sha256(package.outcome.draft)
                   for review in package.reviews)):
        raise ValueError("Исходный текст и его независимые проверки не совпадают")
    old_text, _ = editorial_caption(f"{post.title}\n\n{post.body}\n\nИсточник: {plan.source_url}")
    # old_text_sha256 — хеш точного видимого UTF-8 текста, не JSON-строки.
    if hashlib.sha256(old_text.encode()).hexdigest() != plan.old_text_sha256:
        raise ValueError("Исходный видимый текст изменился")
    visible, entities = editorial_caption(
        f"{plan.title}\n\n{plan.body}\n\nИсточник: {plan.source_url}",
    )
    if len(visible.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Исправление превышает лимит Telegram")
    return {"old_text": old_text, "payload": {
        "chat_id": APPROVED_CHANNELS_BY_CITY[plan.city], "message_id": plan.message_id,
        "text": visible, "entities": list(entities), "link_preview_options": {
            "url": plan.source_url, "prefer_large_media": True, "show_above_text": True,
        }, "reply_markup": map_reply_markup(map_event_url(
            plan.source_url, telegram_app="https://t.me/relocant_media_belgrade_bot", places=True,
        )),
    }}


def execute_event_edit(
    storage: SSHStateClient, content: bytes, plan_sha: str, work: Path,
    client: httpx.Client, token: str, now: datetime,
) -> dict[str, Any]:
    plan = EventPostCorrectionPlan.model_validate_json(content)
    _validate_reviews(plan, now)
    event = reviewed_event(plan)
    expected = event.card(plan.city, "Europe/Belgrade", now)
    feed = read_events_feed(_get_html(client, _FEED).encode())
    if expected.archived or sum(row == expected for row in feed.events) != 1:
        raise ValueError("Проверенная точка ещё не опубликована в Mini App")
    for evidence in plan.evidence:
        soup = BeautifulSoup(_get_html(client, evidence.url), "html.parser")
        for tag in soup.select("script, style, noscript"):
            tag.decompose()
        visible = " ".join(soup.get_text(" ", strip=True).split())
        if " ".join(evidence.text.split()) not in visible:
            raise ValueError("Живой источник изменил проверенные сведения")
    state, state_sha = storage.read()
    validate_ready(state)
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    database = restored(storage, state["snapshot"], work)
    engine = create_engine(f"sqlite:///file:{database.as_posix()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            session.execute(text("PRAGMA query_only=ON"))
            edit = prepared_event_edit(session, plan, event)
    finally:
        engine.dispose()
    payload = edit["payload"]
    current, source = visible_post(_get_html(
        client, f"https://t.me/eduvbelgrad/{plan.message_id}?embed=1",
    ), plan.message_id)
    if source != plan.source_url or current not in {edit["old_text"], payload["text"]}:
        raise ValueError("Публичный пост изменён вне проверенного плана")
    fresh, fresh_sha = storage.read()
    validate_ready(fresh)
    if (fresh, fresh_sha) != (state, state_sha):
        raise ValueError("Каноническое состояние изменилось во время проверки")
    intent = _upload_audit(storage, work, "intent", {
        "version": 3, "operation": "editMessageText", "plan_sha256": plan_sha,
        "canonical_state_sha256": state_sha, "message_id": plan.message_id,
        "edit_payload_sha256": _digest(payload), "checked_at": now.isoformat(),
    })
    print(json.dumps({"correction_intent_sha256": intent}), flush=True)
    receipt: dict[str, Any] = {"version": 3, "plan_sha256": plan_sha,
                               "intent_sha256": intent, "message_id": plan.message_id,
                               "chat_id": payload["chat_id"]}
    try:
        if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token):
            raise ValueError("Некорректный токен Bot API")
        response = client.post(f"https://api.telegram.org/bot{token}/editMessageText",
                               json=payload, timeout=30, follow_redirects=False)
        answer = response.json()
        result = answer.get("result", {})
        if (response.is_success and answer.get("ok") is True
                and result.get("text") == payload["text"]
                and result.get("message_id") == plan.message_id
                and result.get("chat", {}).get("id") == payload["chat_id"]
                and result.get("chat", {}).get("type") == "channel"
                and not any(key in result for key in ("photo", "video", "document", "caption"))
                and sorted(map(canonical_json, result.get("entities", [])))
                == sorted(map(canonical_json, payload["entities"]))
                and result.get("reply_markup") == payload["reply_markup"]):
            receipt.update(status="corrected", map_event_id=expected.id,
                           map_url=payload["reply_markup"]["inline_keyboard"][0][0]["url"])
        elif (response.status_code == 400 and current == payload["text"]
              and answer.get("description", "").startswith("Bad Request: message is not modified")):
            receipt["status"] = "already_corrected"
        else:
            receipt.update(status="uncertain", telegram_status=response.status_code)
    except Exception as error:
        receipt.update(status="uncertain", error_type=type(error).__name__)
    receipt["recorded_at"] = datetime.now(UTC).isoformat()
    receipt_sha = _upload_audit(storage, work, "receipt", receipt)
    return {**receipt, "receipt_sha256": receipt_sha}
