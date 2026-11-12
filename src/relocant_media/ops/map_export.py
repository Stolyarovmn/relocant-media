"""Публичная карта из подтверждённых источников завершённого снимка редакции."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.map_links import map_event_id
from relocant_media.services.local_street_geometry import (
    StreetIndex,
    load_street_index,
    local_street_line,
)
from relocant_media.services.media_review_packet import _valid_snapshot
from relocant_media.services.transport_map import MAP_MODE, SOURCE_ID, matches_transport_notice
from relocant_media.services.transport_map_source import extract_transport_map
from relocant_media.services.utility_map import (
    BVK_NEWS_SOURCE,
    UTILITY_MAP_MODE,
    build_utility_point,
)
from relocant_media.services.utility_scope import extract_power_schedule
from relocant_media.services.water_outage_scope import water_outage_window
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

_ZONE = ZoneInfo("Europe/Belgrade")
_CITY_SOURCE = "city_beograd_news"
_PUBLIC_HOSTS = {"beograd.rs", "www.beograd.rs", "bvk.rs", "www.bvk.rs",
                 "bgprevoz.rs", "www.bgprevoz.rs"}
_ROAD_PERIOD = re.compile(
    r"Период измене: (\d{2}\.\d{2}\.\d{4})[–-](\d{2}\.\d{2}\.\d{4})\.",
)
_MAX_INCIDENTS = 1500
_MAX_FEED_BYTES = 4_000_000
Label = Annotated[str, Field(min_length=1, max_length=260)]
Latitude = Annotated[float, Field(ge=44.2, le=45.2, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=19.8, le=21.1, allow_inf_nan=False)]
Coordinate = tuple[Latitude, Longitude]
Kind = Literal["water", "power", "road"]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Window(PublicModel):
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def ordered(self) -> Window:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("map windows require a timezone")
        if self.ends_at <= self.starts_at:
            raise ValueError("map window end must follow start")
        return self


class Address(Window):
    municipality: Label
    street: Label
    number: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,4}[^\W\d_]?$")]


class Segment(PublicModel):
    municipality: Label
    street: Label
    start: Label
    end: Label


class Pin(PublicModel):
    lat: Latitude
    lon: Longitude
    label: Label


class Line(PublicModel):
    coordinates: Annotated[tuple[Coordinate, ...], Field(min_length=2, max_length=1000)]
    label: Label


class Incident(PublicModel):
    id: Annotated[str, Field(pattern=r"^event_[a-f0-9]{20}$")]
    kind: Kind
    title: Label
    source_url: str
    source_map_url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    published_at: datetime | None = None
    archived: bool = False
    points: Annotated[tuple[Pin, ...], Field(max_length=8)] = ()
    lines: Annotated[tuple[Line, ...], Field(max_length=3)] = ()
    addresses: Annotated[tuple[Address, ...], Field(max_length=300)] = ()
    segments: Annotated[tuple[Segment, ...], Field(max_length=3)] = ()
    windows: Annotated[tuple[Window, ...], Field(max_length=300)] = ()

    @field_validator("source_url", "source_map_url")
    @classmethod
    def official_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.netloc not in _PUBLIC_HOSTS
                or len(value) > 750 or parts.query or parts.fragment
                or any(ord(character) <= 32 for character in value)):
            raise ValueError("map must link to an official source")
        return value

    @model_validator(mode="after")
    def bound_to_source(self) -> Incident:
        if self.id != map_event_id(self.source_url):
            raise ValueError("map id does not match source")
        if self.starts_at is not None and self.ends_at is not None:
            Window(starts_at=self.starts_at, ends_at=self.ends_at)
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("map publication date requires a timezone")
        return self


class MapFeed(PublicModel):
    version: Literal[1] = 1
    generated_at: datetime | None = None
    incidents: Annotated[tuple[Incident, ...], Field(max_length=_MAX_INCIDENTS)] = ()


def read_map_feed(content: bytes) -> MapFeed:
    if len(content) > _MAX_FEED_BYTES:
        raise ValueError("map feed too large")
    return MapFeed.model_validate_json(content)


def write_map_feed(path: Path, feed: MapFeed) -> None:
    content = feed.model_dump_json(exclude_none=True).encode()
    if len(content) > _MAX_FEED_BYTES:
        raise ValueError("map feed too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _coords(latitude: object, longitude: object) -> Coordinate | None:
    if any(type(value) not in {str, int, float} for value in (latitude, longitude)):
        return None
    try:
        lat, lon = float(str(latitude)), float(str(longitude))
    except ValueError:
        return None
    return (lat, lon) if 44.2 <= lat <= 45.2 and 19.8 <= lon <= 21.1 else None


def _line(values: list[list[str]], label: str) -> Line | None:
    coords = [_coords(*value) if isinstance(value, list) and len(value) == 2 else None
              for value in values]
    if None in coords or len(set(coords)) < 2:
        return None
    return Line(coordinates=tuple(pair for pair in coords if pair is not None), label=label)


def _incident(
    document: EvidenceDocument, kind: Kind, title: str, windows: tuple[Window, ...], **extra: Any,
) -> Incident:
    return Incident(
        id=map_event_id(document.url), kind=kind, source_url=document.url,
        title=" ".join(title.split())[:260], published_at=document.published_at,
        starts_at=min((w.starts_at for w in windows), default=None),
        ends_at=max((w.ends_at for w in windows), default=None), windows=windows, **extra,
    )


def _utility(
    document: EvidenceDocument, point: dict[str, Any], title: str | None,
    streets: StreetIndex,
) -> Incident:
    schedule = extract_power_schedule(document)
    geo = point["geography"]
    if schedule:
        addresses = tuple(Address(starts_at=start, ends_at=end, **value)
                          for start, end, entries in schedule for value in entries)
        pins = []
        for value in geo["points"]:
            position = _coords(value["latitude"], value["longitude"])
            if position is not None:
                address = value["address"]
                pins.append(Pin(lat=position[0], lon=position[1],
                                label=f'{address["street"]} {address["number"]}'))
        windows = tuple(Window(starts_at=start, ends_at=end)
                        for start, end in sorted({(start, end) for start, end, _ in schedule}))
        return _incident(document, "power", title or "Плановые отключения света", windows,
                         addresses=addresses, points=tuple(pins))

    period = water_outage_window(document)
    windows = (Window(starts_at=period[0], ends_at=period[1]),) if period else ()
    segments = tuple(Segment.model_validate(value) for value in point["segments"])
    geography = {Segment.model_validate(value["segment"]): value for value in geo["lines"]}
    lines = []
    for segment in segments:
        found = geography.get(segment) or local_street_line(segment.model_dump(), streets)
        if found is not None and (line := _line(found["coordinates"], segment.street)) is not None:
            lines.append(line)
    fallback_title = f"Отключение воды · {segments[0].street}" if segments else document.title
    return _incident(
        document, "water", title or fallback_title, windows, segments=segments,
        lines=tuple(lines), source_map_url=point["official_map_url"],
    )


def _road(
    document: EvidenceDocument, notice: EvidenceDocument, geometry: dict[str, Any],
    title: str | None,
) -> Incident | None:
    if not notice.body or (period := _ROAD_PERIOD.fullmatch(notice.body.splitlines()[0])) is None:
        return None
    try:
        start, end = (datetime.strptime(value, "%d.%m.%Y").date() for value in period.groups())
    except ValueError:
        return None
    if end < start:
        return None
    window = Window(starts_at=datetime.combine(start, time.min, tzinfo=_ZONE),
                    ends_at=datetime.combine(end + timedelta(days=1), time.min, tzinfo=_ZONE))
    points, lines = _road_layers(geometry)
    return (_incident(
        document, "road", title or notice.title, (window,), starts_on=start, ends_on=end,
        source_map_url=notice.url, points=points, lines=lines,
    ) if points or lines else None)


def _road_layers(geometry: dict[str, Any]) -> tuple[tuple[Pin, ...], tuple[Line, ...]]:
    position = _coords(geometry.get("latitude"), geometry.get("longitude"))
    raw_line = geometry.get("line")
    line = (_line(raw_line, "Участок по карте транспортного секретариата")
            if isinstance(raw_line, list) else None)
    pin = (Pin(lat=position[0], lon=position[1], label="Место работ"),) if position else ()
    return pin, (line,) if line is not None else ()


def _source_html(session: Session, item: RawItem) -> tuple[str, Any]:
    snapshot = session.get(RawItemMediaSnapshot, item.id)
    if (snapshot is None or snapshot.source_page != item.url or snapshot.source_html is None
            or not _valid_snapshot(snapshot)):
        return "", None
    return snapshot.source_html, snapshot.manifest.get("utility_geography")


def _published_road(
    session: Session, document: EvidenceDocument, run: EditorialRun, post: Post,
) -> Incident | None:
    native = ((run.outcome.get("_package") or {}).get("presentation") or {}).get("native_map") or {}
    point = native.get("point") or {}
    if (point.get("mode") != MAP_MODE or point.get("source_url") != document.url
            or type(native.get("source_raw_item_id")) is not int):
        return None
    item = session.get(RawItem, native["source_raw_item_id"])
    reviewed = point.get("transport_geometry")
    if not isinstance(reviewed, dict):
        return None
    if item is not None and item.source_id == SOURCE_ID and item.url == point.get("notice_url"):
        notice = EvidenceDocument.model_validate(item, from_attributes=True)
        geometry = extract_transport_map(notice, _source_html(session, item)[0])
        if (geometry is not None and geometry == reviewed
                and matches_transport_notice(document, notice)):
            incident = _road(document, notice, geometry, post.title)
            if incident is not None:
                return incident
    # Старый deep link открывает геометрию уже опубликованной версии как архивную.
    points, lines = _road_layers(reviewed)
    return (_incident(document, "road", post.title, (), archived=True, points=points, lines=lines,
                      source_map_url=point["notice_url"]) if points or lines else None)


def export_map(
    session: Session, *, observed_at: datetime, streets: StreetIndex | None = None,
) -> MapFeed:
    if observed_at.tzinfo is None:
        raise ValueError("snapshot timestamp requires a timezone")
    published = (EditorialRun.delivery_status == "sent") & (Post.telegram_message_id.is_not(None))
    cutoff = observed_at - timedelta(days=21)
    recent = Source.enabled.is_(True) & or_(
        RawItem.ingested_at >= cutoff, RawItem.source_id == SOURCE_ID,
    )
    rows = session.execute(
        select(RawItem, EditorialRun, Post)
        .join(Source, Source.id == RawItem.source_id)
        .outerjoin(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .outerjoin(Post, Post.id == EditorialRun.post_id)
        .where(Source.city == "belgrade", Source.ownership == "official",
               or_(RawItem.source_id.in_((BVK_NEWS_SOURCE, SOURCE_ID)),
                   (RawItem.source_id == _CITY_SOURCE) &
                   or_(RawItem.url.contains("/vodovod-i-kanalizacija/"),
                       RawItem.url.contains("/elektricna-energija/"), published)),
               or_(published, recent))
        .order_by(RawItem.ingested_at.desc(), RawItem.id.desc())
    )
    incidents: dict[str, Incident] = {}
    road_aliases: set[str] = set()
    for item, run, post in rows:
        document = EvidenceDocument.model_validate(item, from_attributes=True)
        sent = run is not None and run.delivery_status == "sent" and post is not None
        title = post.title if sent and post is not None and post.telegram_message_id else None
        if item.source_id == SOURCE_ID:
            geometry = extract_transport_map(document, _source_html(session, item)[0])
            incident = _road(document, document, geometry, None) if geometry else None
            if incident is not None and incident.ends_at is not None and incident.ends_at < cutoff:
                continue
        else:
            html, geography = _source_html(session, item)
            point = build_utility_point(document, html, geography)
            if point is not None and point["mode"] == UTILITY_MAP_MODE:
                incident = _utility(document, point, title, streets or {})
            elif title and run is not None and post is not None:
                incident = _published_road(session, document, run, post)
            else:
                incident = None
        if incident is None:
            continue
        if (incident.kind == "road" and not incident.archived
                and incident.source_map_url != incident.source_url):
            assert incident.source_map_url is not None
            road_aliases.add(incident.source_map_url)
        incidents.setdefault(incident.id, incident)
        if len(incidents) >= _MAX_INCIDENTS:
            break
    return MapFeed(generated_at=observed_at.astimezone(UTC), incidents=tuple(
        value for value in incidents.values() if value.source_url not in road_aliases
    ))


def export_map_snapshot(
    database: Path, output: Path, observed_at: str, *, street_index: Path | None = None,
) -> None:
    streets = load_street_index(street_index) if street_index is not None else None
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            feed = export_map(session, observed_at=datetime.fromisoformat(observed_at),
                              streets=streets)
        write_map_feed(output, feed)
    finally:
        engine.dispose()
