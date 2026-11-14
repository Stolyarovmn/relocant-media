"""Два ограниченных по времени выпуска из официальных данных и опубликованной ленты."""

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.formatting import TELEGRAM_TEXT_LIMIT, formatted_text_units
from relocant_media.collectors.beograd_article import BelgradeArticleCollector
from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.rhmz_forecast import (
    FORECAST_URL,
    WARNING_URL,
    Forecast,
    parse_forecast,
    parse_warning,
)
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.map_links import map_digest_url, map_event_url
from relocant_media.domain.topic_tags import strip_draft_tags
from relocant_media.ops.events_export import export_events
from relocant_media.ops.map_export import (
    Address,
    Incident,
    PrintedRange,
    Segment,
    Settlement,
    _road,
    _water,
    export_map,
)
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.event_place import resolve_event_place
from relocant_media.services.local_street_geometry import load_street_index
from relocant_media.services.road_scope import resolve_road_segments
from relocant_media.services.transport_map import SOURCE_ID, _signature
from relocant_media.services.transport_map_source import _notice_id, extract_transport_map
from relocant_media.services.utility_scope import extract_power_scopes, house_in_range
from relocant_media.services.water_outage_scope import (
    extract_water_outage_scopes,
    reported_water_places,
    water_outage_window,
)
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

_CITY = "belgrade"
_CHANNEL = "https://t.me/eduvbelgrad"
_HISTORY = "digest_receipts"
_PERIOD = re.compile(r"Период измене: (\d{2}\.\d{2}\.\d{4})–(\d{2}\.\d{2}\.\d{4})\.")
_MONTHS = ("января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
           .split())
_PRIORITY = {"transport": 0, "utilities": 1, "immigration": 2, "city": 3, "events": 5}
_LABELS = {"road": "🚧 Перекрытия", "route": "🚋 Транспорт", "weather": "🌦 Погода",
           "water": "💧 Вода", "power": "⚡ Свет", "event": "🎭 Событие"}
_TITLE = {"morning": "Белград сегодня", "evening": "Главное за день",
          "reminder": "Напоминание на завтра"}
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u200D\uFE0F\u20E3]+")
_CIRCULAR_ROUTE = re.compile(r"\b(?:кружна линија|kružna linija)\b", re.I)
_EXISTING_STOPS = re.compile(
    r"\b(?:сва постојећа стајалишта|sva postojeća stajališta)\b", re.I,
)
_MOVED_STOP = re.compile(
    r"(?:стајалиште|stajalište)\s+[„“\"]([^»“”\"]{2,70})[»“”\"]"
    r"[^.;]{0,100}\b(?:се измешта|se izmešta)\b", re.I,
)


class TransportBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=230)
    details: tuple[str, ...] = Field(max_length=12)


class Pilot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    starts_at: datetime
    ends_before: datetime
    morning: time
    evening: time
    delivery_window_minutes: int = Field(ge=5, le=60)
    reminders_from: date | None = None
    official_reminder_sources: dict[date, tuple[str, ...]] = Field(default_factory=dict)
    transport_briefs: dict[str, TransportBrief] = Field(default_factory=dict)


@dataclass(frozen=True)
class Slot:
    kind: Literal["morning", "evening", "reminder"]
    scheduled: datetime
    expires: datetime

    @property
    def key(self) -> str:
        return f"{_CITY}:{self.scheduled.date()}:{self.kind}"


@dataclass(frozen=True)
class Entry:
    label: str
    text: str
    url: str
    link_text: str = "Источник"
    map_id: str = ""
    starts_on: date | None = None
    source_url: str = ""
    reminder: bool = False
    original: int | None = None
    source_id: str = ""
    evidence_sha: str = ""
    place: bool = False
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    address_rows: tuple[str, ...] = ()


@dataclass(frozen=True)
class Digest:
    html: str
    map_url: str | None


def _policy() -> Pilot:
    return Pilot.model_validate(yaml.safe_load(
        Path("cities/belgrade/digests.yaml").read_text(encoding="utf-8"),
    ))


def due_slot(now: datetime) -> Slot | None:
    if now.utcoffset() is None:
        raise ValueError("digest clock requires a timezone")
    policy = _policy()
    zone = ZoneInfo(load_city_profile(_CITY).timezone)
    local = now.astimezone(zone)
    for kind in ("morning", "evening"):
        scheduled = datetime.combine(local.date(), getattr(policy, kind), tzinfo=zone)
        expires = scheduled + timedelta(minutes=policy.delivery_window_minutes)
        if (policy.starts_at <= scheduled.replace(tzinfo=None) < policy.ends_before
                and scheduled <= local < expires):
            return Slot(kind, scheduled, expires)
    if policy.reminders_from is not None and policy.reminders_from <= local.date():
        scheduled = datetime.combine(local.date(), policy.evening, tzinfo=zone)
        expires = scheduled + timedelta(minutes=policy.delivery_window_minutes)
        if scheduled <= local < expires:
            return Slot("reminder", scheduled, expires)
    return None


def _clip_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "…"


def _short(value: str) -> str:
    return _clip_text(value, 230)


def _recap_text(value: str) -> str:
    return " ".join(_EMOJI.sub("", strip_draft_tags(value)).split())


def _day(value: date, *, year: int | None = None) -> str:
    label = f"{value.day} {_MONTHS[value.month - 1]}"
    return f"{label} {value.year} года" if year is not None and value.year != year else label


def _evidence_sha(document: EvidenceDocument) -> str:
    return _content_sha(document.title, document.body)


def _content_sha(title: str, body: str) -> str:
    return hashlib.sha256((title + "\n" + body).encode()).hexdigest()


def _incidents(session: Session, now: datetime, policy: Pilot) -> list[Entry]:
    tomorrow = datetime.combine(now.date() + timedelta(days=1), time.min, now.tzinfo)
    result = []
    streets = load_street_index(Path("cities/belgrade/map/roads.json.gz"))
    feed = export_map(session, observed_at=now, streets=streets)
    for notice in sorted(feed.incidents, key=lambda item: (
        (item.starts_at or tomorrow).astimezone(now.tzinfo).date() != now.date(),
        item.starts_at or tomorrow,
    )):
        if notice.archived or notice.starts_at is None or notice.ends_at is None:
            continue
        if not any(window.starts_at < tomorrow and now < window.ends_at
                   for window in notice.windows):
            continue
        if notice.kind == "road" and (notice.starts_on is None or notice.ends_on is None):
            continue
        text, address_rows = _incident_content(notice, now, tomorrow)
        if notice.kind == "road" and not text:
            source = session.scalar(select(RawItem).where(
                RawItem.source_id == SOURCE_ID, RawItem.url == notice.source_map_url,
            ).order_by(RawItem.ingested_at.desc()).limit(1))
            if source is not None:
                doc = EvidenceDocument.model_validate(source, from_attributes=True)
                segments = [Segment.model_validate(value["segment"])
                            for value in resolve_road_segments(doc, streets)]
                text = _road_text(notice, segments)
        if text or notice.kind == "road" and notice.lines:
            map_id = notice.id if (notice.points or notice.lines or notice.areas
                                  or notice.addresses or notice.segments
                                  or notice.reported_scope) else ""
            starts_on = notice.starts_at.astimezone(now.tzinfo).date()
            url = (notice.source_map_url or notice.source_url
                   if notice.kind == "road" else notice.source_url)
            raw = session.scalar(select(RawItem).join(Source).where(
                RawItem.url == url, Source.city == _CITY, Source.ownership == "official",
                Source.enabled.is_(True),
            ).order_by(RawItem.ingested_at.desc(), RawItem.id.desc()).limit(1))
            raw_document = (EvidenceDocument.model_validate(raw, from_attributes=True)
                            if raw else None)
            if raw_document and notice.kind == "road":
                brief = policy.transport_briefs.get(_evidence_sha(raw_document))
                if brief is not None:
                    text, address_rows = brief.text, brief.details
            elif raw_document and notice.kind == "water" and notice.reported_scope:
                if reported := reported_water_places(raw_document):
                    address_rows = (f"Посёлки: {', '.join(reported[1])}", f"Улицы: {reported[2]}")
            result.append(Entry(_LABELS[notice.kind], text, url, map_id=map_id,
                                starts_on=starts_on, source_url=notice.source_url,
                                source_id=raw_document.source_id if raw_document else "",
                                evidence_sha=_evidence_sha(raw_document) if raw_document else "",
                                address_rows=address_rows))
    return result


def _road_text(notice: Incident, segments: Sequence[Segment]) -> str:
    assert notice.starts_on is not None and notice.ends_on is not None
    scope = "; ".join(f"{row.street}, от {row.start} до {row.end}" for row in segments)
    if not scope:
        return ""
    period = (_day(notice.starts_on) if notice.starts_on == notice.ends_on else
              f"с {_day(notice.starts_on)} по {_day(notice.ends_on, year=notice.starts_on.year)}")
    return f"{_short(scope)}. Период работ — {period}."


def _outage_period(start: datetime, end: datetime, now: datetime) -> str:
    local_start, local_end = start.astimezone(now.tzinfo), end.astimezone(now.tzinfo)
    begin = f"{local_start:%H:%M}"
    finish = f"{local_end:%H:%M}"
    if local_start.date() != now.date():
        begin = f"{_day(local_start.date())} в {begin}"
    if local_end.date() != local_start.date():
        finish = f"{_day(local_end.date())} в {finish}"
    return f"{begin}–{finish}"


def _incident_content(
    notice: Incident, now: datetime, tomorrow: datetime,
) -> tuple[str, tuple[str, ...]]:
    if notice.kind == "road":
        return _road_text(notice, notice.segments), ()
    if notice.reported_scope and notice.starts_at and notice.ends_at:
        district = notice.reported_scope.partition(":")[0]
        period = _outage_period(notice.starts_at, notice.ends_at, now)
        return f"{district} — {period}.", (notice.reported_scope,)
    rows: list[Settlement | Address | PrintedRange] = [
        *notice.settlements, *notice.addresses, *notice.ranges,
    ]
    active = sorted((row for row in rows if row.starts_at < tomorrow and now < row.ends_at),
                    key=lambda row: (row.starts_at, row.ends_at, row.municipality))
    printed_ranges: dict[tuple[str, str, datetime, datetime], list[str]] = {}
    for row in active:
        if isinstance(row, PrintedRange):
            key = row.municipality, row.street, row.starts_at, row.ends_at
            printed_ranges.setdefault(key, []).append(row.number)
    districts: dict[str, None] = {}
    grouped: dict[tuple[str, str, datetime, datetime], list[str]] = {}
    for row in active:
        districts[row.municipality] = None
        if isinstance(row, Settlement):
            place = f"{row.name} ({row.parent})" if row.parent else f"{row.name} целиком"
            number = ""
        else:
            place, number = row.street, row.number
        key = row.municipality, place, row.starts_at, row.ends_at
        if isinstance(row, Address) and any(
            house_in_range(number, part.strip())
            for printed in printed_ranges.get(key, ()) for part in printed.split(",")
        ):
            continue
        grouped.setdefault(key, []).append(number)
    if notice.kind == "water" and len(notice.windows) == 1:
        window = notice.windows[0]
        if window.starts_at < tomorrow and now < window.ends_at:
            for segment in notice.segments:
                districts[segment.municipality] = None
                place = f"{segment.street}, от {segment.start} до {segment.end}"
                grouped.setdefault((segment.municipality, place, window.starts_at,
                                    window.ends_at), [])
    details = tuple(
        f"{district}, {place}{', ' + ', '.join(dict.fromkeys(numbers)) if any(numbers) else ''}"
        f" — {_outage_period(start, end, now)}"
        for (district, place, start, end), numbers in grouped.items()
    )
    return (_short(", ".join(districts)) + ".", details) if details else ("", ())


def _routes(session: Session, today: date, policy: Pilot) -> list[Entry]:
    result = []
    items = session.scalars(select(RawItem).join(Source).where(
        Source.id == SOURCE_ID, Source.city == _CITY, Source.enabled.is_(True),
        Source.ownership == "official",
    ).order_by(RawItem.ingested_at.desc(), RawItem.id.desc()).limit(70))
    for item in items:
        doc = EvidenceDocument.model_validate(item, from_attributes=True)
        if _notice_id(doc) is None:
            continue
        period = _PERIOD.fullmatch(item.body.partition("\n")[0])
        if period is None:
            continue
        try:
            start, end = (datetime.strptime(value, "%d.%m.%Y").date() for value in period.groups())
        except ValueError:
            continue
        if not start <= today <= end:
            continue
        _, routes, _, _ = _signature(item.title, item.body, today.year)
        if not routes:
            continue
        numbers = ", ".join(sorted(routes)[:8]).upper()
        period_text = f"до {_day(end, year=today.year)}"
        brief = policy.transport_briefs.get(_evidence_sha(doc))
        if brief is not None:
            text, details = brief.text, brief.details
        elif len(routes) == 1 and _CIRCULAR_ROUTE.search(item.body):
            text = f"Линия {numbers} работает по кольцевому маршруту {period_text}."
            details = (("На изменённом участке сохраняются все остановки.",)
                       if _EXISTING_STOPS.search(item.body) else ())
        elif len(routes) == 1 and (moved := _MOVED_STOP.findall(" ".join(item.body.split()))):
            moved = list(dict.fromkeys(moved))
            stops = ", ".join(f"«{stop.strip()}»" for stop in moved[:4])
            text = f"На маршруте {numbers} {period_text} изменены остановки: {stops}."
            details = ()
        else:
            continue
        result.append(Entry(_LABELS["route"], text, item.url, starts_on=start,
                            source_id=doc.source_id, evidence_sha=_evidence_sha(doc),
                            address_rows=details))
    return sorted(result, key=lambda entry: entry.starts_on != today)


def morning_entries(session: Session, now: datetime, forecast: Forecast | None,
                    warning: str | None = None) -> list[Entry]:
    policy = _policy()
    notices = _incidents(session, now, policy)
    roads = [item for item in notices if item.label == _LABELS["road"]]
    routes = _routes(session, now.date(), policy)
    road_maps = {item.url: item.map_id for item in roads if item.map_id}
    routes = [replace(item, map_id=road_maps.get(item.url, "")) for item in routes]
    route_url = routes[0].url if routes else None
    roads = [item for item in roads if item.text and item.url != route_url]
    routes = [_link_to_published(session, entry, now) for entry in routes]
    roads = [_link_to_published(session, entry, now) for entry in roads]
    utility = [_link_to_published(session, entry, now)
               for entry in notices if entry.label != _LABELS["road"]]
    if warning:
        weather = [Entry(_LABELS["weather"], warning, WARNING_URL, "РХМЗ / MeteoAlarm")]
        return (weather + roads[:1] + routes[:1] + utility + roads[1:2])[:4]
    weather = [Entry(_LABELS["weather"], forecast.text, FORECAST_URL, "РХМЗ")] if forecast else []
    if forecast and forecast.hazardous:
        return (weather + roads[:1] + routes[:1] + utility + roads[1:2])[:4]
    return (roads[:1] + routes[:1] + weather + utility + roads[1:2])[:4]


def evening_entries(session: Session, now: datetime) -> list[Entry]:
    since = datetime.combine(now.date(), time.min, now.tzinfo)
    posts = session.execute(select(Post, RawItem.url)
        .join(EditorialRun, EditorialRun.post_id == Post.id)
        .join(RawItem, RawItem.id == EditorialRun.raw_item_id).where(
        Post.city == _CITY, Post.telegram_message_id > 0, Post.published_at >= since,
        Post.published_at <= now, EditorialRun.delivery_status == "sent",
        ~Post.title.startswith("[тестовый запуск]"),
    ).order_by(Post.published_at.desc()).limit(30)).all()
    seen = set()
    entries = []
    for post, source_url in sorted(posts, key=lambda row: _PRIORITY.get(row[0].category, 4)):
        if post.body.lstrip().startswith("Реклама\n") or post.title.startswith("Реклама"):
            continue
        title = _short(_recap_text(post.title))
        key = re.sub(r"[^\w]+", " ", title.casefold()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        lead = _recap_text(post.body.partition("\n\n")[0])
        if (len(lead) > 240 or len(lead.split()) > 36 or post.body.lstrip().startswith("#")
                or "https://" in lead or lead.casefold().rstrip(".") == title.casefold()):
            lead = ""
        entries.append(Entry(title, lead, f"{_CHANNEL}/{post.telegram_message_id}", "Подробнее",
                             source_url=source_url))
    return entries[:4]


def _tomorrow_entries(session: Session, now: datetime) -> list[Entry]:
    tomorrow = now.date() + timedelta(days=1)
    tomorrow_start = datetime.combine(tomorrow, time.min, now.tzinfo)
    policy = _policy()
    entries = {entry.url: entry for entry in _incidents(session, tomorrow_start, policy)
               if entry.starts_on == tomorrow and entry.text}
    # Preserve a separately requested announcement if the normal scan is unusually busy.
    for source in policy.official_reminder_sources.get(tomorrow, ()):
        candidate = entries.pop(source, None)
        entries = {source: candidate or Entry(_LABELS["water"], "", source, starts_on=tomorrow,
                                              source_id="city_beograd_news"), **entries}
    for event in export_events(session, observed_at=now).events:
        if (event.archived or event.sponsored
                or event.starts_at.astimezone(now.tzinfo).date() != tomorrow):
            continue
        raw = session.scalar(select(RawItem).join(Source).where(
            RawItem.url == event.source_url, Source.city == _CITY, Source.ownership == "official",
            Source.enabled.is_(True),
        ).order_by(RawItem.ingested_at.desc(), RawItem.id.desc()).limit(1))
        if raw is not None:
            document = EvidenceDocument.model_validate(raw, from_attributes=True)
            entries.setdefault(raw.url, Entry(
                _LABELS["event"], event.title, raw.url, map_id=event.id,
                starts_on=tomorrow, source_id=raw.source_id,
                evidence_sha=_evidence_sha(document), place=True,
                original=int(event.post_url.rsplit("/", 1)[-1]),
                scheduled_start=event.starts_at, scheduled_end=event.ends_at,
            ))
    return list(entries.values())


def _original_post(
    session: Session, url: str, *, as_of: datetime | None = None, evidence_sha: str = "",
) -> int | None:
    query = select(Post.telegram_message_id, RawItem.title, RawItem.body).join(
        EditorialRun, EditorialRun.post_id == Post.id,
    ).join(RawItem, RawItem.id == EditorialRun.raw_item_id).where(
        Post.city == _CITY, EditorialRun.delivery_status == "sent", RawItem.url == url,
        Post.telegram_message_id > 0, ~Post.title.startswith("[тестовый запуск]"),
    )
    if as_of is None:
        query = query.order_by(Post.published_at.asc(), Post.id.asc())
    else:
        # Do not point today's schedule at a post about an earlier revision of the same URL.
        query = query.where(
            Post.published_at <= as_of,
            EditorialRun.evidence["title"].as_string() == RawItem.title,
            EditorialRun.evidence["body"].as_string() == RawItem.body,
        ).order_by(Post.published_at.desc(), Post.id.desc())
    if not evidence_sha:
        return session.scalars(query.limit(1)).first()
    for message_id, title, body in session.execute(query).tuples():
        if _content_sha(title, body) == evidence_sha:
            return message_id
    return None


def _link_to_published(session: Session, entry: Entry, now: datetime) -> Entry:
    for url in dict.fromkeys((entry.url, entry.source_url or entry.url)):
        if message_id := _original_post(session, url, as_of=now, evidence_sha=entry.evidence_sha):
            return replace(entry, url=f"{_CHANNEL}/{message_id}", link_text="Подробнее")
    return entry


async def _confirm_reminder(client: httpx.AsyncClient, entry: Entry) -> Entry | None:
    # The registry selects the parser. URL identity is checked again after the network call.
    if entry.starts_on is None or not re.fullmatch(r"[a-z0-9_]+", entry.source_id):
        return None
    registry = Path(f"cities/belgrade/sources/{entry.source_id}.yaml")
    if not registry.is_file():
        return None
    config = load_source_file(registry)
    if config.ownership != "official" or config.extraction.status != "active":
        return None
    zone = ZoneInfo("Europe/Belgrade")
    day_start = datetime.combine(entry.starts_on, time.min, zone)
    next_day = day_start + timedelta(days=1)
    try:
        async with asyncio.timeout(14):
            collector = (BelgradeArticleCollector(config, client)
                         if entry.source_id in {"city_beograd_news", "city_beograd"}
                         else build_collector(config, client))
            exact = getattr(collector, "collect_url", None)
            if not callable(exact):
                return None
            live = await exact(entry.url)
    except (TimeoutError, httpx.HTTPError, ValueError):
        return None
    if live.url != entry.url or live.source_id != entry.source_id:
        return None
    document = EvidenceDocument.model_validate(live, from_attributes=True)
    same = _evidence_sha(document) == entry.evidence_sha
    # If address or timing changed, an old map button could open stale boundaries.
    # Hold these for the revision loop unless the announcement is the explicitly
    # configured water source and supplies its full current scope.
    text, address_rows = entry.text, entry.address_rows
    if entry.place:
        place = resolve_event_place(document, live.source_html or "",
                                    profile=load_city_profile(_CITY))
        if place is None or not same:
            return None
        starts = datetime.fromisoformat(place["starts_at"]).astimezone(zone)
        end = datetime.fromisoformat(place["ends_at"]) if place["ends_at"] else None
        if starts.date() != entry.starts_on or (starts, end) != (
            entry.scheduled_start, entry.scheduled_end,
        ):
            return None
        text = f"{entry.text} · {starts:%H:%M}, {place['venue']['name']}."
    elif entry.label == _LABELS["water"]:
        period = water_outage_window(document)
        scopes = extract_water_outage_scopes(document)
        if period is None and not scopes:
            return None
        notice = _water(document, None, None, {}, None, scopes)
        if notice.starts_at is None or notice.starts_at.astimezone(zone).date() != entry.starts_on:
            return None
        reported = reported_water_places(document)
        if reported is not None and period is not None:
            starts, ends = period
            text = f"{reported[0]} — {starts:%H:%M}–{ends:%H:%M}."
            address_rows = (f"Посёлки: {', '.join(reported[1])}", f"Улицы: {reported[2]}")
        elif same:
            updated_text, address_rows = _incident_content(notice, day_start, next_day)
            text = updated_text or text
        else:
            return None
    elif entry.label == _LABELS["power"]:
        schedule = extract_power_scopes(document)
        if (not same or not schedule
                or min(row.starts_at for row in schedule).date() != entry.starts_on):
            return None
    elif entry.label == _LABELS["road"]:
        geometry = extract_transport_map(document, live.source_html or "")
        restriction = _road(document, document, geometry, None, {})
        if not same or restriction is None or restriction.starts_on != entry.starts_on:
            return None
    else:
        return None
    return replace(entry, text=_short(text), address_rows=address_rows,
                   map_id=entry.map_id if same else "", reminder=True)


def render(slot: Slot, entries: list[Entry], telegram_app: str | None = None) -> Digest | None:
    if not entries or (len(entries) < 2 and slot.kind != "morning"
                       and not any(entry.reminder for entry in entries)):
        return None
    reminder_only = bool(entries) and all(entry.reminder for entry in entries)
    title = _TITLE["reminder"] if reminder_only else _TITLE[slot.kind]
    day = slot.scheduled.date() + timedelta(days=1 if reminder_only else 0)
    lines = [f"<b>{title} · {_day(day)}</b>"]
    details: dict[int, tuple[str, tuple[str, ...]]] = {}
    used_emoji = 0
    for entry in entries[:4]:
        label = entry.label
        if label in _LABELS.values():
            used_emoji += 1
            if used_emoji > 3:
                label = label.partition(" ")[2]
        if entry.reminder and not reminder_only:
            label = f"Завтра · {label}"
        text = f" — {escape(entry.text)}" if entry.text else ""
        original = (f' · <a href="{_CHANNEL}/{entry.original}">Первый пост</a>'
                    if entry.original else "")
        location = ""
        if entry.place and entry.map_id:
            url = map_event_url(entry.url, telegram_app=telegram_app, places=True)
            location = f' · <a href="{escape(url, quote=True)}">На карте</a>'
        label_html = f"<b>{escape(label)}</b>"
        link = f' <a href="{escape(entry.url, quote=True)}">{entry.link_text}</a>'
        if entry.label in {_LABELS["road"], _LABELS["route"]} and entry.link_text == "Источник":
            label_html = f'<a href="{escape(entry.url, quote=True)}">{label_html}</a>'
            link = ""
        lines.append(f'{label_html}{text}{link}'
                     f'{original}{location}')
        if entry.address_rows:
            heading = ("Адреса и время работ" if entry.label in {_LABELS["power"], _LABELS["water"]}
                       else "Подробнее")
            details[len(lines) - 1] = (heading, entry.address_rows)
    tag = ("#Коммуналка" if reminder_only and all(
        item.label in {_LABELS["water"], _LABELS["power"]} for item in entries
    ) else "#Сводка")
    lines.append(tag)
    units = formatted_text_units("\n\n".join(lines))
    remaining_rows = sum(len(rows) for _, rows in details.values())
    remaining_units = TELEGRAM_TEXT_LIMIT - units
    for index, (title, rows) in details.items():
        heading = f"<b>{title}</b>"
        overflow = "Полный список — по ссылке выше."
        budget = remaining_units * len(rows) // max(1, remaining_rows)
        remaining_rows -= len(rows)
        remaining = budget - formatted_text_units(heading + overflow) - 4
        quote = [heading]
        for row in rows:
            line = f"• {escape(row)}"
            remaining -= formatted_text_units(line) + 1
            if remaining < 0:
                quote.append(overflow)
                break
            quote.append(line)
        quote_html = "\n<blockquote expandable>" + "\n".join(quote) + "</blockquote>"
        quote_units = formatted_text_units(quote_html)
        if len(quote) > 1 and budget >= quote_units:
            lines[index] += quote_html
            remaining_units -= quote_units
    return Digest("\n\n".join(lines), map_digest_url(
        tuple(dict.fromkeys(item.map_id for item in entries[:4] if item.map_id and not item.place)),
        telegram_app=telegram_app,
    ))


def _cover(slot: Slot) -> tuple[Path, str] | None:
    base = Path("cities/belgrade/digest-covers")
    try:
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        options = manifest[slot.kind]
        if not options:
            return None
        index = (slot.scheduled.date() - date(2026, 11, 13)).days % len(options)
        item = options[index]
        name = item["file"]
        if not re.fullmatch(r"(?:morning|evening)-[0-9]{2}\.jpg", name):
            return None
        path = base / name
        TelegramBotAPI._photo(path)
        url = item["url"]
        if not url.startswith("https://commons.wikimedia.org/wiki/File:"):
            return None
        credit = f'<a href="{escape(url, quote=True)}">Фото: {escape(item["artist"])}</a>'
        return path, f'{credit} · {escape(item["license"])} · кадрировано'
    except (OSError, ValueError, TypeError, KeyError):
        return None


Commit = Callable[[dict[str, Any], str, dict[str, Any]], tuple[dict[str, Any], str]]


async def deliver(slot: Slot, digest: Digest, state: dict[str, Any], sha: str, commit: Commit,
                  send: Callable[[], Awaitable[int]]) -> dict[str, Any]:
    receipts = state.get(_HISTORY, {})
    if slot.key in receipts:
        return cast(dict[str, Any], receipts[slot.key])
    intent = {"state": "sending", "scheduled_at": slot.scheduled.isoformat(),
              "html_sha256": hashlib.sha256(digest.html.encode()).hexdigest()}
    claimed, claimed_sha = commit(state, sha, {_HISTORY: {**receipts, slot.key: intent}})
    receipt: dict[str, Any]
    try:
        message_id = await send()
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("invalid Telegram receipt")
    except Exception:
        receipt = {**intent, "state": "uncertain"}
    else:
        receipt = {**intent, "state": "sent", "message_id": message_id}
    commit(claimed, claimed_sha, {_HISTORY: {**receipts, slot.key: receipt}})
    return receipt


async def run(database: Path, slot: Slot, state: dict[str, Any], sha: str, commit: Commit,
              environment: dict[str, str]) -> dict[str, Any]:
    if slot.key in state.get(_HISTORY, {}):
        return cast(dict[str, Any], state[_HISTORY][slot.key])
    async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
        now = datetime.now(UTC).astimezone(slot.scheduled.tzinfo)
        forecast = None
        warning = None
        if slot.kind == "morning":
            try:
                response = await client.get(FORECAST_URL)
                forecast = parse_forecast(response.raise_for_status().text, now)
            except httpx.HTTPError:
                pass
            try:
                response = await client.get(WARNING_URL)
                warning = parse_warning(response.raise_for_status().content, now)
            except httpx.HTTPError:
                pass
        engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
        try:
            with Session(engine) as session:
                entries = (
                    morning_entries(session, now, forecast, warning) if slot.kind == "morning"
                    else evening_entries(session, now) if slot.kind == "evening" else []
                )
                candidates = _tomorrow_entries(session, now) if slot.kind != "morning" else []
                originals = {entry.url: entry.original or _original_post(
                    session, entry.source_url or entry.url,
                ) for entry in candidates}
        finally:
            engine.dispose()
        reply_to = None
        confirmed: list[Entry] = []
        for offset in range(0, min(12, len(candidates)), 4):
            confirmed.extend(entry for entry in await asyncio.gather(*(
                _confirm_reminder(client, candidate) for candidate in candidates[offset:offset + 4]
            )) if entry is not None)
            if len(confirmed) >= 4:
                break
        confirmed = confirmed[:4]
        priority_url = next(iter(_policy().official_reminder_sources.get(
            now.date() + timedelta(days=1), (),
        )), None)
        priority_missing = bool(priority_url and all(row.url != priority_url for row in confirmed))
        if (priority_missing or candidates and not confirmed) and (
            len(entries) < 2 or now < slot.expires - timedelta(minutes=15)
        ):
            return {"state": "reminder_source_unconfirmed", "items": len(candidates)}
        if confirmed:
            confirmed = [replace(item, original=originals[item.url]) for item in confirmed]
            if len(confirmed) == 1 and (slot.kind == "reminder" or len(entries) < 2):
                reply_to = confirmed[0].original
            entries = confirmed + entries[:max(0, 4 - len(confirmed))]
        digest = render(slot, entries, environment.get("RELOCANT_MAP_TELEGRAM_APP"))
        photo = None
        if digest is not None and slot.kind != "reminder" and not all(
            entry.reminder for entry in entries
        ):
            cover = _cover(slot)
            if cover is not None:
                photo, credit = cover
                caption = digest.html + "\n\n" + credit
                if formatted_text_units(caption) > 1024:
                    compact = [
                        replace(item, text="" if item.link_text == "Подробнее"
                                and item.label not in _LABELS.values()
                                else _clip_text(item.text, 125))
                        for item in entries
                    ]
                    shortened = render(slot, compact, environment.get("RELOCANT_MAP_TELEGRAM_APP"))
                    if shortened is not None:
                        caption = shortened.html + "\n\n" + credit
                if formatted_text_units(caption) <= 1024:
                    digest = Digest(caption, digest.map_url)
                else:
                    photo = None
        if digest is None:
            return {"state": "insufficient_items", "items": len(entries)}
        if datetime.now(UTC) >= slot.expires:
            return {"state": "window_closed"}
        bot = TelegramBotAPI(environment["TELEGRAM_BOT_TOKEN"], client)
        return await deliver(slot, digest, state, sha, commit, lambda: bot.send_digest_message(
            APPROVED_CHANNEL_ID, digest.html, map_url=digest.map_url,
            reply_to=reply_to, photo=photo,
        ))
