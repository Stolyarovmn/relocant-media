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
from relocant_media.bot.formatting import formatted_text_units
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
from relocant_media.services.utility_scope import extract_power_scopes
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


class Pilot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    starts_at: datetime
    ends_before: datetime
    morning: time
    evening: time
    delivery_window_minutes: int = Field(ge=5, le=60)
    reminders_from: date | None = None
    official_reminder_sources: dict[date, tuple[str, ...]] = Field(default_factory=dict)


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
    return hashlib.sha256((document.title + "\n" + document.body).encode()).hexdigest()


def _incidents(session: Session, now: datetime) -> list[Entry]:
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
        text = _incident_text(notice, now, tomorrow)
        if notice.kind == "road" and not text:
            source = session.scalar(select(RawItem).where(
                RawItem.source_id == SOURCE_ID, RawItem.url == notice.source_map_url,
            ).order_by(RawItem.ingested_at.desc()).limit(1))
            if source is not None:
                doc = EvidenceDocument.model_validate(source, from_attributes=True)
                segments = [Segment.model_validate(value["segment"])
                            for value in resolve_road_segments(doc, streets)]
                text = _road_text(notice, segments)
        if text:
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
            result.append(Entry(_LABELS[notice.kind], text, url, map_id=map_id,
                                starts_on=starts_on, source_url=notice.source_url,
                                source_id=raw_document.source_id if raw_document else "",
                                evidence_sha=_evidence_sha(raw_document) if raw_document else ""))
    return result


def _road_text(notice: Incident, segments: Sequence[Segment]) -> str:
    assert notice.starts_on is not None and notice.ends_on is not None
    scope = "; ".join(f"{row.street}, от {row.start} до {row.end}" for row in segments)
    if not scope:
        return ""
    period = (_day(notice.starts_on) if notice.starts_on == notice.ends_on else
              f"с {_day(notice.starts_on)} по {_day(notice.ends_on, year=notice.starts_on.year)}")
    return f"{_short(scope)}. Период работ — {period}."


def _incident_text(notice: Incident, now: datetime, tomorrow: datetime) -> str:
    if notice.kind == "road":
        return _road_text(notice, notice.segments)
    if notice.reported_scope and notice.starts_at and notice.ends_at:
        begin = notice.starts_at.astimezone(now.tzinfo)
        finish = notice.ends_at.astimezone(now.tzinfo)
        district = notice.reported_scope.partition(":")[0]
        return f"{district} — {begin:%H:%M}–{finish:%H:%M}."
    rows: list[Settlement | Address | PrintedRange] = [
        *notice.settlements, *notice.addresses, *notice.ranges,
    ]
    active = [row for row in rows if row.starts_at < tomorrow and now < row.ends_at]
    if not active and notice.kind == "water" and notice.segments:
        segment = notice.segments[0]
        window = next((item for item in notice.windows
                       if item.starts_at < tomorrow and now < item.ends_at), None)
        if window is not None:
            start_local = window.starts_at.astimezone(now.tzinfo)
            end_local = window.ends_at.astimezone(now.tzinfo)
            period = (f"{start_local:%H:%M}–{end_local:%H:%M}"
                      if start_local.date() == end_local.date() else
                      f"{start_local:%d.%m %H:%M}–{end_local:%d.%m %H:%M}")
            return (f"{segment.municipality}, {segment.street}, от {segment.start} до "
                    f"{segment.end} — {period}.")
    if not active:
        return ""
    first = active[0]
    local_start = first.starts_at.astimezone(now.tzinfo)
    local_end = first.ends_at.astimezone(now.tzinfo)
    start = f"{local_start:%H:%M}"
    end = f"{local_end:%H:%M}"
    if local_start.date() != now.date():
        start = f"{_day(local_start.date())} в {start}"
    if local_end.date() != now.date():
        end = f"{_day(local_end.date())} в {end}"
    if isinstance(first, Settlement):
        place = f"{first.name} ({first.parent})" if first.parent else first.name
    else:
        place = f"{first.street}, {first.number}"
    rest = " Другие адреса и часы — в объявлении." if len(active) > 1 else ""
    return f"{first.municipality}, {place} — {start}–{end}.{rest}"


def _routes(session: Session, today: date, excluded: set[str]) -> list[Entry]:
    result = []
    items = session.scalars(select(RawItem).join(Source).where(
        Source.id == SOURCE_ID, Source.city == _CITY, Source.enabled.is_(True),
        Source.ownership == "official",
    ).order_by(RawItem.ingested_at.desc(), RawItem.id.desc()).limit(70))
    for item in items:
        doc = EvidenceDocument.model_validate(item, from_attributes=True)
        if item.url in excluded or _notice_id(doc) is None:
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
        _, routes, _, stops = _signature(item.title, item.body, today.year)
        if not routes:
            continue
        numbers = ", ".join(sorted(routes)[:8]).upper()
        stop_text = (f" В объявлении указаны остановки {', '.join(sorted(stops)[:5])}."
                     if stops else "")
        subject = f"на линии {numbers}" if len(routes) == 1 else f"на линиях {numbers}"
        text = (f"Изменения {subject} по {_day(end, year=today.year)}."
                f"{stop_text} Схема движения — в объявлении.")
        result.append(Entry(_LABELS["route"], text, item.url, starts_on=start))
    return sorted(result, key=lambda item: item.starts_on != today)


def morning_entries(session: Session, now: datetime, forecast: Forecast | None,
                    warning: str | None = None) -> list[Entry]:
    notices = _incidents(session, now)
    routes = _routes(session, now.date(), {item.url for item in notices})
    roads = [item for item in notices if item.label == _LABELS["road"]]
    utility = [item for item in notices if item.label != _LABELS["road"]]
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
    entries = {entry.url: entry for entry in _incidents(session, tomorrow_start)
               if entry.starts_on == tomorrow}
    # Preserve a separately requested announcement if the normal scan is unusually busy.
    for source in _policy().official_reminder_sources.get(tomorrow, ()):
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


def _original_post(session: Session, url: str) -> int | None:
    return session.scalar(select(Post.telegram_message_id).join(
        EditorialRun, EditorialRun.post_id == Post.id,
    ).join(RawItem, RawItem.id == EditorialRun.raw_item_id).where(
        Post.city == _CITY, EditorialRun.delivery_status == "sent", RawItem.url == url,
        Post.telegram_message_id > 0,
    ).order_by(Post.published_at.asc(), Post.id.asc()).limit(1))


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
    text = entry.text
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
            text = (f"{reported[0]} — {starts:%H:%M}–{ends:%H:%M}. "
                    f"Посёлки: {', '.join(reported[1])}.")
        elif same:
            text = _incident_text(notice, day_start, next_day) or text
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
    return replace(entry, text=_short(text), map_id=entry.map_id if same else "", reminder=True)


def render(slot: Slot, entries: list[Entry], telegram_app: str | None = None) -> Digest | None:
    if not entries or (len(entries) < 2 and slot.kind != "morning"
                       and not any(entry.reminder for entry in entries)):
        return None
    reminder_only = bool(entries) and all(entry.reminder for entry in entries)
    title = _TITLE["reminder"] if reminder_only else _TITLE[slot.kind]
    day = slot.scheduled.date() + timedelta(days=1 if reminder_only else 0)
    lines = [f"<b>{title} · {_day(day)}</b>"]
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
        lines.append(f'<b>{escape(label)}</b>{text} '
                     f'<a href="{escape(entry.url, quote=True)}">{entry.link_text}</a>'
                     f'{original}{location}')
    tag = ("#Коммуналка" if reminder_only and all(
        item.label in {_LABELS["water"], _LABELS["power"]} for item in entries
    ) else "#Сводка")
    return Digest("\n\n".join([*lines, tag]), map_digest_url(
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
                        replace(item, text="" if item.link_text == "Подробнее" else
                                _clip_text(item.text, 125))
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
