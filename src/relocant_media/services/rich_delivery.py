"""Общий транспорт проверенной карты для автоматической и одобренной владельцем доставки."""

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any, Protocol, cast

from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.bot.api import PhotoUpload, TelegramBotAPI, map_reply_markup
from relocant_media.bot.formatting import (
    TELEGRAM_TEXT_LIMIT,
    editorial_message_html,
    formatted_text_units,
    rich_editorial_html,
)
from relocant_media.bot.publisher import Publisher
from relocant_media.domain.map_links import map_event_url
from relocant_media.services.native_map import (
    LEGACY_NOTICE_LAYOUT,
    NOTICE_COVER,
    NOTICE_LAYOUT,
)
from relocant_media.services.notice_banner import render_notice_banner
from relocant_media.services.production_media import PreparedProductionMedia
from relocant_media.services.transport_map import MAP_MODE
from relocant_media.services.transport_map_image import render_transport_map
from relocant_media.services.utility_map import UTILITY_MAP_MODE


class RichMessageBot(Protocol):
    async def send_rich_message(
        self, chat_id: int | str, html: str, *, photo: PhotoUpload | None = None,
        map_url: str | None = None, reply_to: int | None = None,
    ) -> int: ...

    async def send_notice_message(
        self, chat_id: int | str, html: str, *, source_url: str, map_url: str | None = None,
        reply_to: int | None = None,
    ) -> int: ...


@dataclass(frozen=True)
class PreparedNativeMessage:
    html: str
    photo: PhotoUpload | None = None
    image_sha256: str | None = None
    map_url: str | None = None
    source_preview_url: str | None = None
    reviewed_html_sha256: str | None = None


def prepare_native_transport(
    package: EditorialPackage, publisher: Publisher, media: PreparedProductionMedia | None,
    *, editorial_text: str,
) -> tuple[RichMessageBot, PreparedNativeMessage] | None:
    presentation = package.presentation
    place = getattr(presentation, "event_place", None)
    if place is not None:
        if datetime.fromisoformat(place["expires_at"]) <= datetime.now(UTC):
            raise ValueError("event is over")
        if media is not None and len(media.uploads) != 1:
            return None  # An already approved album keeps its reviewed multi-photo delivery.
        return _prepare_event_transport(place, publisher, media, editorial_text)
    native = presentation.native_map if presentation is not None else None
    if native is None:
        return None
    layout = native.get("layout")
    notice_layout = layout in {NOTICE_LAYOUT, LEGACY_NOTICE_LAYOUT}
    if media is not None and not notice_layout:
        raise ValueError("native map and attached media require a combined review")
    expected_text = (f"{package.outcome.draft.title}\n\n{package.outcome.draft.body}"
                     f"\n\nИсточник: {native['point']['source_url']}")
    if expected_text != editorial_text:
        raise ValueError("native map cannot drop delivery-specific text")
    bot = getattr(publisher, "bot", None)
    html = native["message_html"]
    point = native["point"]
    source_preview = (layout == NOTICE_LAYOUT and media is None
                      and formatted_text_units(html) <= TELEGRAM_TEXT_LIMIT)
    method = "send_notice_message" if source_preview else "send_rich_message"
    if not callable(getattr(bot, method, None)):
        raise ValueError("production publisher does not support reviewed native map")
    if notice_layout:
        reviewed_html_sha256 = None
        notice_url = map_event_url(
            point["source_url"], telegram_app=os.environ.get("RELOCANT_MAP_TELEGRAM_APP"),
        )
        if layout == NOTICE_LAYOUT:
            if NOTICE_COVER in html or "<tg-map" in html:
                raise ValueError("notice has unsupported content")
            if source_preview:
                return cast(RichMessageBot, bot), PreparedNativeMessage(
                    html, map_url=notice_url, source_preview_url=point["source_url"],
                )
            reviewed_html_sha256 = native["message_html_sha256"]
            html = rich_editorial_html(html)
            if media is None:
                return cast(RichMessageBot, bot), PreparedNativeMessage(
                    html, map_url=notice_url, reviewed_html_sha256=reviewed_html_sha256,
                )
            html = f'<figure><img src="{NOTICE_COVER}"/></figure>' + html
        if html.count(NOTICE_COVER) != 1 or "<tg-map" in html:
            raise ValueError("reviewed notice needs one cover and no inline map")
        notice_photo, notice_sha256 = _notice_photo(native, media, editorial_text)
        if media is not None and media.caption != editorial_text:
            credits = media.caption.removeprefix(editorial_text).strip().splitlines()
            html += "".join(f"<p>{escape(credit)}</p>" for credit in credits)
        return cast(RichMessageBot, bot), PreparedNativeMessage(
            html, notice_photo, notice_sha256, notice_url,
            reviewed_html_sha256=reviewed_html_sha256,
        )

    geometry = point.get("transport_geometry")
    line = geometry.get("line") if point.get("mode") == MAP_MODE else None
    filename = "transport-map.png"
    if point.get("mode") == UTILITY_MAP_MODE and point.get("image_sha256") is not None:
        line = point["geography"]["lines"][0]["coordinates"]
        filename = "outage-map.png"
    photo = None
    image_sha256 = None
    if line:
        content = render_transport_map(line)
        image_sha256 = hashlib.sha256(content).hexdigest()
        if point.get("image_sha256") != image_sha256:
            raise ValueError("map image does not match reviewed bytes")
        photo = PhotoUpload(filename, content)
        # Все локальные ошибки вложения должны возникнуть до долговечной отметки sending.
        if TelegramBotAPI._photo(photo)[2] != "image/png":
            raise ValueError("map image must be PNG")
    elif point.get("image_sha256") is not None:
        raise ValueError("reviewed map image requires a nonempty source line")
    references = html.count("tg://photo?id=transport_map")
    if references != (1 if photo is not None else 0):
        raise ValueError("map image and reviewed HTML do not match")
    map_url = None
    if point.get("mode") in (MAP_MODE, UTILITY_MAP_MODE):
        map_url = map_event_url(
            point["source_url"], telegram_app=os.environ.get("RELOCANT_MAP_TELEGRAM_APP"),
        )
    return cast(RichMessageBot, bot), PreparedNativeMessage(html, photo, image_sha256, map_url)


def _prepare_event_transport(
    place: dict[str, Any], publisher: Publisher, media: PreparedProductionMedia | None,
    editorial_text: str,
) -> tuple[RichMessageBot, PreparedNativeMessage]:
    formatted = editorial_message_html(editorial_text)
    if formatted is None or formatted[1] != place["source_url"]:
        raise ValueError("event map requires the exact reviewed source")
    html, source_url = formatted
    map_url = map_event_url(source_url, telegram_app=os.environ.get("RELOCANT_MAP_TELEGRAM_APP"),
                            places=True)
    source_preview = media is None and formatted_text_units(html) <= TELEGRAM_TEXT_LIMIT
    bot = getattr(publisher, "bot", None)
    method = "send_notice_message" if source_preview else "send_rich_message"
    if not callable(getattr(bot, method, None)):
        raise ValueError("production publisher does not support event map")
    transport = cast(RichMessageBot, bot)
    if source_preview:
        return transport, PreparedNativeMessage(
            html, map_url=map_url, source_preview_url=source_url,
        )
    html = rich_editorial_html(html)
    if media is None:
        return transport, PreparedNativeMessage(html, map_url=map_url)
    photo, digest = _notice_photo({}, media, editorial_text)
    credits = media.caption.removeprefix(editorial_text).strip().splitlines()
    html = (f'<figure><img src="{NOTICE_COVER}"/></figure>{html}'
            + "".join(f"<p>{escape(credit)}</p>" for credit in credits))
    return transport, PreparedNativeMessage(html, photo, digest, map_url)


def _notice_photo(
    native: dict[str, object], media: PreparedProductionMedia | None, editorial_text: str,
) -> tuple[PhotoUpload, str]:
    if media is not None:
        if (len(media.uploads) != 1 or len(media.release.assets) != 1
                or not (media.caption == editorial_text
                        or media.caption.startswith(editorial_text + "\n\n"))):
            raise ValueError("the reviewed notice requires exactly one bound cover and its credits")
        photo = media.uploads[0]
        digest = hashlib.sha256(photo.data).hexdigest()
        if digest != media.release.assets[0].sha256:
            raise ValueError("notice cover differs from reviewed media")
    else:
        kind = native.get("cover_kind")
        if not isinstance(kind, str) or kind not in {"water", "power", "road"}:
            raise ValueError("unknown notice cover category")
        content = render_notice_banner(kind)
        digest = hashlib.sha256(content).hexdigest()
        if native.get("cover_sha256") != digest:
            raise ValueError("notice banner does not match reviewed bytes")
        photo = PhotoUpload("notice-cover.png", content)
    TelegramBotAPI._photo(photo)
    return photo, digest


def rich_receipt(
    prepared: PreparedNativeMessage | str, *, state: str, message_id: int | None = None,
) -> dict[str, object]:
    value = PreparedNativeMessage(prepared) if isinstance(prepared, str) else prepared
    receipt: dict[str, object] = {
        "state": state, "message_html_sha256": hashlib.sha256(value.html.encode()).hexdigest(),
        "planned_message_count": 1, "message_ids": [message_id] if message_id else []}
    if value.image_sha256 is not None:
        receipt["image_sha256"] = value.image_sha256
    if value.source_preview_url is not None:
        receipt["source_preview_url"] = value.source_preview_url
    if value.reviewed_html_sha256 is not None:
        receipt["reviewed_html_sha256"] = value.reviewed_html_sha256
    if value.map_url is not None:
        receipt["map_url"] = value.map_url
        reply_markup = canonical_json(map_reply_markup(value.map_url))
        receipt["reply_markup_sha256"] = hashlib.sha256(reply_markup.encode()).hexdigest()
    return receipt


async def send_native_message(
    bot: RichMessageBot, target: int, prepared: PreparedNativeMessage | str, *,
    reply_to: int | None = None,
) -> int:
    """Одна попытка; неопределённый результат обрабатывает общая долговечная квитанция."""
    value = PreparedNativeMessage(prepared) if isinstance(prepared, str) else prepared
    if reply_to is not None:
        html = value.html + (f'<p><a href="https://t.me/eduvbelgrad/{reply_to}">'
                             "Первый пост</a></p>" if value.source_preview_url is None else
                             f'\n\n<a href="https://t.me/eduvbelgrad/{reply_to}">Первый пост</a>')
        if value.source_preview_url is not None:
            return await bot.send_notice_message(target, html, source_url=value.source_preview_url,
                                                 map_url=value.map_url, reply_to=reply_to)
        return await bot.send_rich_message(target, html, photo=value.photo, map_url=value.map_url,
                                           reply_to=reply_to)
    if value.source_preview_url is not None:
        if value.map_url is None or value.photo is not None:
            raise ValueError("notice preview needs a map link and no attached photo")
        message_id = await bot.send_notice_message(
            target, value.html, source_url=value.source_preview_url, map_url=value.map_url,
        )
    elif value.map_url is not None:
        message_id = await bot.send_rich_message(
            target, value.html, photo=value.photo, map_url=value.map_url,
        )
    elif value.photo is None:
        message_id = await bot.send_rich_message(target, value.html)
    else:
        message_id = await bot.send_rich_message(target, value.html, photo=value.photo)
    if type(message_id) is not int or message_id <= 0:
        raise ValueError("rich publisher returned an invalid receipt")
    return message_id
