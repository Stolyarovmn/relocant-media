"""Ручной повтор последнего объявления с проверенным оформлением Mini App."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.gemini_process import IsolatedGeminiProvider
from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.package import (
    EditorialPackage,
    canonical_json,
    context_sha256,
    evaluate_package,
)
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.map_links import map_event_id
from relocant_media.ops.map_export import read_map_feed
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.native_map import (
    NOTICE_LAYOUT,
    bind_native_map_draft,
    build_native_map,
    verify_native_map_snapshot,
)
from relocant_media.services.owner_review import _package_sha256
from relocant_media.services.rich_delivery import (
    PreparedNativeMessage,
    prepare_native_transport,
    rich_receipt,
    send_native_message,
)
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

CHANNEL = "eduvbelgrad"
_CHANNEL_URL = f"https://t.me/s/{CHANNEL}"
_FEED_URL = "https://stolyarovmn.github.io/relocant-media/map/incidents.json"
_HISTORY = "map_republications"
_Phrase = Annotated[str, Field(min_length=1, max_length=240)]


class Replacement(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    before: _Phrase
    after: _Phrase


class Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    message_id: Annotated[int, Field(gt=0, le=2_147_483_647)]
    event_id: Annotated[str, Field(pattern=r"^event_[0-9a-f]{20}$")]
    replacement: Replacement | None = None


def latest_message_id(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for node in soup.select(".tgme_widget_message[data-post]"):
        channel, _, raw_id = str(node["data-post"]).rpartition("/")
        if channel == CHANNEL and raw_id.isascii() and raw_id.isdigit():
            ids.append(int(raw_id))
    if not ids:
        raise ValueError("channel preview contains no messages")
    return max(ids)


def _check_public_target(client: httpx.Client, request: Request, *, active: bool) -> None:
    page = client.get(_CHANNEL_URL)
    page.raise_for_status()
    if latest_message_id(page.text) != request.message_id:
        raise ValueError("requested message is no longer the latest")
    if active:
        snapshot = client.get(_FEED_URL)
        snapshot.raise_for_status()
        events = read_map_feed(snapshot.content).incidents
        event = next((item for item in events if item.id == request.event_id), None)
        if (event is None or event.archived or event.ends_at is None
                or event.ends_at <= datetime.now(UTC)
                or not (event.lines or event.areas or event.points)):
            raise ValueError("map event is absent, finished or has no confirmed geometry")


def prepare(session: Session, request: Request) -> EditorialPackage:
    post, run, raw, source = session.execute(
        select(Post, EditorialRun, RawItem, Source)
        .join(EditorialRun, EditorialRun.post_id == Post.id)
        .join(RawItem, RawItem.id == EditorialRun.raw_item_id)
        .join(Source, Source.id == RawItem.source_id)
        .where(Post.city == "belgrade", Post.telegram_message_id == request.message_id,
               Post.published_at.is_not(None), EditorialRun.delivery_status == "sent"),
    ).one()
    package = EditorialPackage.model_validate_json(canonical_json(run.outcome["_package"]))
    old = package.presentation.native_map if package.presentation else None
    receipt = run.outcome.get("_rich_delivery")
    draft = package.outcome.draft
    if (old is None or package.presentation is None or package.event is None
            or package.raw_item_id != raw.id or package.evidence.url != raw.url
            or old.get("point", {}).get("source_url") != raw.url
            or map_event_id(raw.url) != request.event_id or source.ownership != "official"
            or not source.enabled or post.title != draft.title or post.body != draft.body
            or run.outcome.get("_package_sha256") != _package_sha256(package)
            or not isinstance(receipt, dict) or receipt.get("state") != "sent"
            or receipt.get("message_ids") != [request.message_id]
            or receipt.get("message_html_sha256") != old.get("message_html_sha256")):
        raise ValueError("canonical package and delivered post do not match")
    profile = load_city_profile(package.city)
    documents = tuple(package.event.documents)
    document_ids = [item.raw_item_id for item in documents]
    native = build_native_map(session, document_ids, documents, source_url=raw.url, profile=profile)
    if native is None or native.get("layout") != NOTICE_LAYOUT:
        raise ValueError("post has no verified new map layout")
    verify_native_map_snapshot(session, native, documents, document_ids)
    if request.replacement is not None:
        before, after = request.replacement.before, request.replacement.after
        if draft.body.count(before) != 1 or before == after:
            raise ValueError("the correction does not match the canonical text exactly once")
        draft = draft.model_copy(update={"body": draft.body.replace(before, after, 1)})
    presentation = bind_native_map_draft(
        package.presentation.model_copy(update={"native_map": native}), draft,
    )
    placeholders = tuple(
        review.model_copy(update={"verdict": "hold"}) for review in package.reviews
    )
    return package.model_copy(update={
        "presentation": presentation, "reviews": placeholders,
        "outcome": package.outcome.model_copy(update={"draft": draft}),
    })


async def deliver(
    request: Request, state: dict[str, Any], sha: str,
    write: Callable[[dict[str, Any], str], str],
    send: Callable[[], Awaitable[int]], message: PreparedNativeMessage, context: str,
) -> dict[str, Any]:
    receipts = state.get(_HISTORY, {})
    key = str(request.message_id)
    if key in receipts:
        previous = receipts[key]
        if (not isinstance(previous, dict) or previous.get("state") != "sent"
                or previous.get("event_id") != request.event_id):
            raise ValueError("previous attempt requires reconciliation")
        return previous
    intent = {**rich_receipt(message, state="sending"), "original_id": request.message_id,
              "event_id": request.event_id, "context_sha256": context}
    claimed = {**state, _HISTORY: {**receipts, key: intent}}
    claimed_sha = write(claimed, sha)
    try:
        message_id = await send()
        if type(message_id) is not int or message_id <= request.message_id:
            raise ValueError("Telegram did not confirm a new message ID")
    except Exception:
        uncertain = {**intent, "state": "uncertain"}
        write({**claimed, _HISTORY: {**receipts, key: uncertain}}, claimed_sha)
        raise RuntimeError("publication outcome is uncertain; no automatic retry") from None
    sent = {**intent, **rich_receipt(message, state="sent", message_id=message_id)}
    write({**claimed, _HISTORY: {**receipts, key: sent}}, claimed_sha)
    return sent


def apply(
    database: Path, request: Request, state: dict[str, Any], sha: str,
    write: Callable[[dict[str, Any], str], str], environment: Mapping[str, str],
) -> None:
    from relocant_media.ops.server_cycle import ChannelPublisher, fetch_documents

    previous = state.get(_HISTORY, {}).get(str(request.message_id))
    if previous is not None:
        if previous["state"] != "sent" or previous["event_id"] != request.event_id:
            raise ValueError("previous attempt requires reconciliation")
        message_id = previous["message_ids"][0]
        print(json.dumps({"republication": "already_sent", "message_id": message_id}), flush=True)
        return
    with httpx.Client(timeout=25, trust_env=False, headers={"Cache-Control": "no-cache"}) as client:
        _check_public_target(client, request, active=True)
        print(json.dumps({"republication": "target_verified"}), flush=True)
        engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
        try:
            with Session(engine) as session:
                package = prepare(session, request)
        finally:
            engine.dispose()
        print(json.dumps({"republication": "canonical_package_prepared"}), flush=True)
        provider = IsolatedGeminiProvider(
            environment["GEMINI_API_KEY"], model=environment.get("GEMINI_MODEL") or
            "gemini-3.5-flash-lite", timeout_seconds=55,
        )
        package = reviewed_package(package, provider, lambda: fetch_documents(package))
        evaluate_package(package)
        if not all(review.verdict == "approve" and review.score >= 75
                   and not review.flags and review.live_source_checked
                   for review in package.reviews):
            print(json.dumps({
                "republication": "review_held",
                "reviews": [review.verdict for review in package.reviews],
            }), flush=True)
            raise ValueError("independent reviews did not approve the republished package")
        _check_public_target(client, request, active=True)
        print(json.dumps({"republication": "independent_reviews_passed"}), flush=True)
    draft = package.outcome.draft
    text = f"{draft.title}\n\n{draft.body}\n\nИсточник: {package.evidence.url}"

    async def send() -> int:
        async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
            bot = TelegramBotAPI(environment["TELEGRAM_BOT_TOKEN"], client)
            prepared = prepare_native_transport(package, ChannelPublisher(bot), None,
                                                editorial_text=text)
            if prepared is None or not prepared[1].map_url or not prepared[1].photo:
                raise ValueError("republication requires a cover and a map link")
            transport, message = prepared
            record = await deliver(
                request, state, sha, write,
                lambda: send_native_message(transport, APPROVED_CHANNEL_ID, message),
                message, context_sha256(package),
            )
            message_id = record["message_ids"][0]
            assert type(message_id) is int
            return message_id

    print(json.dumps({"republication": "sent", "message_id": asyncio.run(send())}), flush=True)
