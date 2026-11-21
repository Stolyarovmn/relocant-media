"""Добавить кнопки карты к выбранным опубликованным постам из проверенного снимка."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.domain.map_links import map_event_id, map_event_url
from relocant_media.ops.events_export import EventsFeed, export_events, read_events_feed
from relocant_media.ops.map_export import MapFeed, read_map_feed
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.storage.models import EditorialRun, Post, RawItem, Submission

_FEED_URL = "https://stolyarovmn.github.io/relocant-media/map/incidents.json"
_EVENTS_URL = "https://stolyarovmn.github.io/relocant-media/map/events.json"
_ENTRY = re.compile(r"([1-9][0-9]{0,9}):(event_[0-9a-f]{20})")


def parse_targets(value: str) -> dict[int, str]:
    entries = value.split(",")
    if not 1 <= len(entries) <= 5:
        raise ValueError("expected one to five post and event pairs")
    targets = {}
    for entry in entries:
        match = _ENTRY.fullmatch(entry.strip())
        if match is None or int(match[1]) in targets:
            raise ValueError("invalid or repeated post and event pair")
        targets[int(match[1])] = match[2]
    return targets


def _owner_confirmed(session: Session, post: Post, run: EditorialRun, message_id: int) -> bool:
    package = run.outcome.get("_package_sha256")
    if post.category != "utilities" or not isinstance(package, str) or not re.fullmatch(
        r"[0-9a-f]{64}", package,
    ):
        return False
    receipts = list(session.scalars(select(Submission).where(
        Submission.kind == "owner_editorial_review", Submission.status == "published",
        Submission.payload["raw_item_id"].as_integer() == run.raw_item_id,
        Submission.payload["post_id"].as_integer() == post.id,
        Submission.payload["telegram_channel_message_id"].as_integer() == message_id,
    ).limit(2)))
    return (len(receipts) == 1 and receipts[0].payload.get("owner_decision") == "publish"
            and receipts[0].payload.get("package_sha256") == package)


def plan(
    session: Session, targets: Mapping[int, str], feed: MapFeed, telegram_app: str,
) -> dict[int, str]:
    events = {event.id: event for event in feed.incidents}
    posts = session.execute(
        select(Post, EditorialRun, RawItem)
        .join(EditorialRun, EditorialRun.post_id == Post.id)
        .join(RawItem, RawItem.id == EditorialRun.raw_item_id)
        .where(Post.city == "belgrade", Post.published_at.is_not(None),
               Post.telegram_message_id.in_(targets), EditorialRun.delivery_status == "sent"),
    )
    buttons: dict[int, str] = {}
    for post, run, raw in posts:
        message_id = post.telegram_message_id
        assert message_id is not None
        event_id = targets[message_id]
        event = events.get(event_id)
        receipt = run.outcome.get("_rich_delivery")
        rich_confirmed = (isinstance(receipt, dict) and receipt.get("state") == "sent"
                          and receipt.get("message_ids") == [message_id])
        if (message_id in buttons or event is None or event.source_url != raw.url
                or event_id != map_event_id(raw.url)
                or not (event.lines or event.points or event.areas or event.addresses)):
            raise ValueError("published post, delivery receipt and map event must match")
        if not rich_confirmed and (receipt is not None or event.kind not in {"water", "power"}
                                   or not _owner_confirmed(session, post, run, message_id)):
            raise ValueError("published post, delivery receipt and map event must match")
        url = map_event_url(raw.url, telegram_app=telegram_app)
        old_url = receipt.get("map_url") if isinstance(receipt, dict) else None
        if not url.startswith("https://t.me/") or old_url not in (None, url):
            raise ValueError("configured Telegram link or historical markup differs")
        buttons[message_id] = url
    if buttons.keys() != targets.keys():
        raise ValueError("confirmed publication missing from snapshot")
    return {message_id: buttons[message_id] for message_id in targets}


def plan_event_buttons(
    session: Session, targets: Mapping[int, str], feed: EventsFeed, telegram_app: str,
) -> dict[int, str]:
    snapshot = export_events(session, observed_at=datetime.now(UTC))
    reviewed = {event.id: event for event in snapshot.events}
    published = {event.id: event for event in feed.events}
    buttons = {}
    for message_id, event_id in targets.items():
        selected = {key: event for key, event in reviewed.items()
                    if event.id == event_id or event.group_id == event_id}
        live = {key: event for key, event in published.items()
                if event.id == event_id or event.group_id == event_id}
        if (not selected or selected != live or all(event.archived for event in selected.values())
                or any(event.post_url != f"https://t.me/eduvbelgrad/{message_id}"
                       for event in selected.values())):
            raise ValueError("published event and confirmed afisha card must match")
        event = next(iter(selected.values()))
        url = map_event_url(event.source_url, telegram_app=telegram_app, places=True)
        if not url.startswith("https://t.me/"):
            raise ValueError("configured Telegram application is required")
        buttons[message_id] = url
    return buttons


async def apply(database: Path, targets: Mapping[int, str], telegram_app: str, token: str) -> None:
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        response = await client.get(_FEED_URL, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        feed = read_map_feed(response.content)
        incident_ids = {event.id for event in feed.incidents}
        incident_targets = {key: value for key, value in targets.items() if value in incident_ids}
        event_targets = {key: value for key, value in targets.items() if value not in incident_ids}
        events = EventsFeed()
        if event_targets:
            response = await client.get(_EVENTS_URL, headers={"Cache-Control": "no-cache"})
            response.raise_for_status()
            events = read_events_feed(response.content)
        engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
        try:
            with Session(engine) as session:
                buttons = plan(session, incident_targets, feed, telegram_app)
                if event_targets:
                    buttons.update(plan_event_buttons(session, event_targets, events, telegram_app))
        finally:
            engine.dispose()
        bot = TelegramBotAPI(token, client)
        for message_id, url in buttons.items():
            changed = await bot.edit_map_button(APPROVED_CHANNEL_ID, message_id, url)
            status = "updated" if changed else "already_set"
            print(f"map_button message_id={message_id} status={status}", flush=True)
