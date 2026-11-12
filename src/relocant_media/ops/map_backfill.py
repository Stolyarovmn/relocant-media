"""Добавить кнопки карты к выбранным опубликованным постам из проверенного снимка."""

import re
from collections.abc import Mapping
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.domain.map_links import map_event_id, map_event_url
from relocant_media.ops.map_export import MapFeed, read_map_feed
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.storage.models import EditorialRun, Post, RawItem

_FEED_URL = "https://stolyarovmn.github.io/relocant-media/map/incidents.json"
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
        if (message_id in buttons or event is None or event.source_url != raw.url
                or event_id != map_event_id(raw.url)
                or not (event.lines or event.points or event.addresses)
                or not isinstance(receipt, dict) or receipt.get("state") != "sent"
                or receipt.get("message_ids") != [message_id]):
            raise ValueError("published post, delivery receipt and map event must match")
        url = map_event_url(raw.url, telegram_app=telegram_app)
        if not url.startswith("https://t.me/") or receipt.get("map_url") not in (None, url):
            raise ValueError("configured Telegram link or historical markup differs")
        buttons[message_id] = url
    if buttons.keys() != targets.keys():
        raise ValueError("confirmed publication missing from snapshot")
    return {message_id: buttons[message_id] for message_id in targets}


async def apply(database: Path, targets: Mapping[int, str], telegram_app: str, token: str) -> None:
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        response = await client.get(_FEED_URL, headers={"Cache-Control": "no-cache"})
        response.raise_for_status()
        feed = read_map_feed(response.content)
        engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
        try:
            with Session(engine) as session:
                buttons = plan(session, targets, feed, telegram_app)
        finally:
            engine.dispose()
        bot = TelegramBotAPI(token, client)
        for message_id, url in buttons.items():
            changed = await bot.edit_map_button(APPROVED_CHANNEL_ID, message_id, url)
            status = "updated" if changed else "already_set"
            print(f"map_button message_id={message_id} status={status}", flush=True)
