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
from relocant_media.services.local_outage_geometry import (
    AreaIndex,
    AreaPolygon,
    load_area_index,
    within_outage_segment,
)
from relocant_media.services.local_street_geometry import (
    StreetIndex,
    load_street_index,
    local_street_line,
)
from relocant_media.services.media_review_packet import _valid_snapshot
from relocant_media.services.road_scope import is_road_restriction, resolve_road_segments
from relocant_media.services.transport_map import MAP_MODE, SOURCE_ID, matches_transport_notice
from relocant_media.services.transport_map_source import extract_transport_map
from relocant_media.services.utility_geography import _name as geography_name
from relocant_media.services.utility_map import (
    BVK_NEWS_SOURCE,
    UTILITY_MAP_MODE,
    build_utility_point,
)
from relocant_media.services.utility_scope import (
    PowerScope,
    SettlementScope,
    extract_power_scopes,
    house_in_range,
    is_explicit_house,
    settlement_matches_context,
)
from relocant_media.services.water_outage_scope import (
    WaterScope,
    extract_water_outage_scopes,
    reported_water_places,
    water_outage_window,
)
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
_MAX_HOUSES = 1200
_MAX_AREAS = 650
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


class PrintedRange(Window):
    municipality: Label
    street: Label
    number: Label


class Settlement(Window):
    municipality: Label
    name: Label
    parent: Label | None = None


class Pin(PublicModel):
    lat: Latitude
    lon: Longitude
    label: Label
    address: Address | None = None
    coordinate_source: Literal["rgz"] | None = None
    approximate: bool = False


Ring = Annotated[tuple[Coordinate, ...], Field(min_length=4, max_length=1000)]


class Area(Window):
    scope: Literal["building", "settlement"]
    rings: Annotated[tuple[Ring, ...], Field(min_length=1, max_length=16)]
    label: Label
    address: Address | None = None

    @field_validator("rings")
    @classmethod
    def closed(cls, rings: tuple[Ring, ...]) -> tuple[Ring, ...]:
        if any(ring[0] != ring[-1] or len(set(ring)) < 3 for ring in rings):
            raise ValueError("map polygon rings must be closed")
        return rings

    @model_validator(mode="after")
    def tied_to_address(self) -> Area:
        if (self.scope == "building") != (self.address is not None):
            raise ValueError("only building polygons require an address")
        if self.address is not None and (
            self.starts_at != self.address.starts_at or self.ends_at != self.address.ends_at
        ):
            raise ValueError("building and address windows must agree")
        return self


class Line(PublicModel):
    coordinates: Annotated[tuple[Coordinate, ...], Field(min_length=2, max_length=1000)]
    label: Label
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def ordered_if_timed(self) -> Line:
        if self.starts_at is not None and self.ends_at is not None:
            Window(starts_at=self.starts_at, ends_at=self.ends_at)
        elif self.starts_at is not None or self.ends_at is not None:
            raise ValueError("map line requires both ends of its window")
        return self


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
    points: Annotated[tuple[Pin, ...], Field(max_length=_MAX_HOUSES)] = ()
    lines: Annotated[tuple[Line, ...], Field(max_length=3)] = ()
    areas: Annotated[tuple[Area, ...], Field(max_length=_MAX_AREAS)] = ()
    addresses: Annotated[tuple[Address, ...], Field(max_length=_MAX_HOUSES)] = ()
    ranges: Annotated[tuple[PrintedRange, ...], Field(max_length=_MAX_HOUSES)] = ()
    settlements: Annotated[tuple[Settlement, ...], Field(max_length=120)] = ()
    reported_scope: Label | None = None
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


def _line(values: list[list[str]], label: str, window: Window | None = None) -> Line | None:
    coords = [_coords(*value) if isinstance(value, list) and len(value) == 2 else None
              for value in values]
    if None in coords or len(set(coords)) < 2:
        return None
    return Line(coordinates=tuple(pair for pair in coords if pair is not None), label=label,
                starts_at=window.starts_at if window else None,
                ends_at=window.ends_at if window else None)


def _incident(
    document: EvidenceDocument, kind: Kind, title: str, windows: tuple[Window, ...], **extra: Any,
) -> Incident:
    return Incident(
        id=map_event_id(document.url), kind=kind, source_url=document.url,
        title=" ".join(title.split())[:260], published_at=document.published_at,
        starts_at=min((w.starts_at for w in windows), default=None),
        ends_at=max((w.ends_at for w in windows), default=None), windows=windows, **extra,
    )


def _key(address: Address | dict[str, str]) -> tuple[str, ...]:
    values = address.model_dump() if isinstance(address, Address) else address
    return tuple(geography_name(values[field]) for field in ("municipality", "street", "number"))


def _area(
    polygon: AreaPolygon, window: Window, label: str, address: Address | None = None,
) -> Area:
    return Area(rings=polygon.rings, scope="building" if address else "settlement", label=label,
                address=address, starts_at=window.starts_at, ends_at=window.ends_at)


def _settlement(
    scope: SettlementScope, window: Window, index: AreaIndex | None,
) -> tuple[Settlement, list[Area]]:
    entry = Settlement(municipality=scope.municipality, name=scope.name, parent=scope.parent,
                       starts_at=window.starts_at, ends_at=window.ends_at)
    polygons = index.settlement(scope.name, scope.municipality) if index is not None else ()
    return entry, [_area(polygon, window, scope.name) for polygon in polygons]


def _covered_by_settlement(
    address: Address, position: Coordinate | None, settlements: list[Settlement],
    index: AreaIndex | None,
) -> bool:
    municipality = address.municipality.split(",", 1)[0]
    for place in settlements:
        if (address.starts_at != place.starts_at or address.ends_at != place.ends_at
                or geography_name(municipality) != geography_name(place.municipality)):
            continue
        scope = SettlementScope(place.municipality, place.name, place.parent)
        if settlement_matches_context(scope, address.municipality):
            return True
        if (index is not None and position is not None
                and index.settlement_contains(place.name, place.municipality, position)):
            return True
    return False


def _power(
    document: EvidenceDocument, schedule: list[PowerScope], point: dict[str, Any] | None,
    title: str | None, index: AreaIndex | None,
) -> Incident:
    windows = tuple(Window(starts_at=start, ends_at=end) for start, end in sorted({
        (scope.starts_at, scope.ends_at) for scope in schedule
    }))
    geocoded = {
        _key(entry["address"]): _coords(entry["latitude"], entry["longitude"])
        for entry in (point["geography"]["points"] if point is not None else ())
    }
    addresses: dict[tuple[str, ...], Address] = {}
    ranges: set[PrintedRange] = set()
    settlements: list[Settlement] = []
    areas: list[Area] = []
    for scope in schedule:
        window = Window(starts_at=scope.starts_at, ends_at=scope.ends_at)
        if scope.settlement is not None:
            entry, polygons = _settlement(scope.settlement, window, index)
            if entry not in settlements:
                settlements.append(entry)
                areas.extend(polygons)
        for value in scope.addresses:
            number = value["number"]
            street, municipality = value["street"], value["municipality"]
            numbers: tuple[str, ...]
            if is_explicit_house(number):
                numbers = (number,)
            else:
                ranges.add(PrintedRange(
                    starts_at=scope.starts_at, ends_at=scope.ends_at,
                    municipality=municipality, street=street, number=number,
                ))
                locations = index.street(street, municipality) if index else ()
                numbers = tuple(item.number for item in locations
                                if house_in_range(item.number, number))
            for number in numbers:
                address = Address(
                    starts_at=scope.starts_at, ends_at=scope.ends_at,
                    municipality=municipality, street=street, number=number,
                )
                key = (*_key(address), scope.starts_at.isoformat(), scope.ends_at.isoformat())
                addresses.setdefault(key, address)
    pins: list[Pin] = []
    houses: list[Address] = []
    for address in addresses.values():
        if len(houses) >= _MAX_HOUSES:
            break
        label = f"{address.street}, {address.number}"
        location = (index.address(address.street, address.number, address.municipality)
                    if index is not None else None)
        position = location.point if location is not None else geocoded.get(_key(address))
        if _covered_by_settlement(address, position, settlements, index):
            continue
        houses.append(address)
        if location is not None and location.buildings and len(areas) < _MAX_AREAS:
            areas.extend(_area(polygon, address, label, address) for polygon in location.buildings)
        elif position is not None:
            pins.append(Pin(lat=position[0], lon=position[1], label=label, address=address,
                            coordinate_source="rgz" if location and location.osm_url is None
                            else None))
    return _incident(
        document, "power", title or "Плановые отключения света", windows,
        addresses=tuple(houses), points=tuple(pins), areas=tuple(areas[:_MAX_AREAS]),
        ranges=tuple(sorted(ranges, key=lambda item: (item.starts_at, item.municipality,
                                                      item.street, item.number))),
        settlements=tuple(settlements[:120]),
    )


def _water(
    document: EvidenceDocument, point: dict[str, Any] | None, title: str | None,
    streets: StreetIndex, index: AreaIndex | None, scopes: tuple[WaterScope, ...],
) -> Incident:
    period = water_outage_window(document)
    periods = {(scope.starts_at, scope.ends_at) for scope in scopes}
    if period is not None:
        periods.add(period)
    windows = tuple(Window(starts_at=start, ends_at=end) for start, end in sorted(periods))
    geography = {Segment.model_validate(value["segment"]): value
                 for value in (point["geography"]["lines"] if point else ())}
    segment_windows: dict[tuple[Segment, Window | None], None] = {}
    addresses: dict[Address, Coordinate | None] = {}
    areas: list[Area] = []
    settlements: list[Settlement] = []
    for scope in scopes:
        window = Window(starts_at=scope.starts_at, ends_at=scope.ends_at)
        for place in scope.settlements:
            place_entry, polygons = _settlement(place, window, index)
            if place_entry not in settlements:
                settlements.append(place_entry)
                areas.extend(polygons)
        for segment_entry in scope.segments:
            segment_windows[(Segment.model_validate(segment_entry), window)] = None
        for address_entry in scope.addresses:
            address = Address(
                starts_at=window.starts_at, ends_at=window.ends_at,
                municipality=address_entry["municipality"], street=address_entry["street"],
                number=address_entry["number"],
            )
            addresses[address] = None
    if not segment_windows and point is not None:
        fallback_window = Window(starts_at=period[0], ends_at=period[1]) if period else None
        segment_windows.update({(Segment.model_validate(entry), fallback_window): None
                                for entry in point["segments"]})
    lines: list[Line] = []
    segments = tuple(dict.fromkeys(segment for segment, _ in segment_windows))[:3]
    for segment, line_window in segment_windows:
        found = geography.get(segment) or local_street_line(segment.model_dump(), streets)
        if found is not None and len(lines) < 3 and (
            line := _line(found["coordinates"], segment.street, line_window)
        ) is not None:
            lines.append(line)
            if index is not None and line_window is not None:
                for street_location in index.street(segment.street, segment.municipality):
                    if not within_outage_segment(street_location.point, line.coordinates):
                        continue
                    address = Address(**line_window.model_dump(),
                                      municipality=segment.municipality, street=segment.street,
                                      number=street_location.number)
                    addresses.setdefault(address, street_location.point)
    pins: list[Pin] = []
    houses: list[Address] = []
    for address, segment_position in sorted(addresses.items(), key=lambda item: _key(item[0])):
        if len(houses) >= _MAX_HOUSES:
            break
        location = (index.address(address.street, address.number, address.municipality)
                    if index is not None else None)
        position = location.point if location is not None else segment_position
        if _covered_by_settlement(address, position, settlements, index):
            continue
        houses.append(address)
        label = f"{address.street}, {address.number}"
        if location is not None and location.buildings:
            areas.extend(_area(polygon, address, label, address) for polygon in location.buildings)
        elif segment_position is None and position is not None:
            pins.append(Pin(lat=position[0], lon=position[1], label=label, address=address,
                            coordinate_source="rgz" if location and location.osm_url is None
                            else None))
    reported = reported_water_places(document)
    reported_scope = None
    if reported is not None:
        municipality, names, street_text = reported
        reported_scope = f"{municipality}: {', '.join(names)}; улицы: {street_text}"[:260]
        if index is not None:
            for name in names:
                if position := index.settlement_reference(name, municipality):
                    pins.append(Pin(lat=position[0], lon=position[1], label=name, approximate=True))
    fallback_title = (f"Отключение воды · {reported[0]}" if reported else
                      f"Отключение воды · {segments[0].street}" if segments else document.title)
    return _incident(
        document, "water", title or fallback_title, windows, segments=segments,
        lines=tuple(lines), source_map_url=point["official_map_url"] if point else None,
        addresses=tuple(houses), points=tuple(pins[:_MAX_HOUSES]), areas=tuple(areas[:_MAX_AREAS]),
        settlements=tuple(settlements[:120]), reported_scope=reported_scope,
    )


def _road(
    document: EvidenceDocument, notice: EvidenceDocument, geometry: dict[str, Any] | None,
    title: str | None, streets: StreetIndex,
) -> Incident | None:
    if (not is_road_restriction(notice) or not notice.body
            or (period := _ROAD_PERIOD.fullmatch(notice.body.splitlines()[0])) is None):
        return None
    try:
        start, end = (datetime.strptime(value, "%d.%m.%Y").date() for value in period.groups())
    except ValueError:
        return None
    if end < start:
        return None
    window = Window(starts_at=datetime.combine(start, time.min, tzinfo=_ZONE),
                    ends_at=datetime.combine(end + timedelta(days=1), time.min, tzinfo=_ZONE))
    lines = _road_lines(geometry)
    segments = []
    if not lines:
        for resolved in resolve_road_segments(notice, streets):
            segment = Segment.model_validate(resolved["segment"])
            line = _line(resolved["coordinates"], segment.street)
            if line is not None:
                lines.append(line)
                segments.append(segment)
    return _incident(
        document, "road", title or notice.title, (window,), starts_on=start, ends_on=end,
        source_map_url=notice.url, lines=tuple(lines[:3]), segments=tuple(segments[:3]),
    )


def _road_lines(geometry: dict[str, Any] | None) -> list[Line]:
    raw_line = geometry.get("line") if geometry is not None else None
    line = (_line(raw_line, "Участок по карте транспортного секретариата")
            if isinstance(raw_line, list) else None)
    return [line] if line is not None else []


def _source_html(session: Session, item: RawItem) -> tuple[str, Any]:
    snapshot = session.get(RawItemMediaSnapshot, item.id)
    if (snapshot is None or snapshot.source_page != item.url or snapshot.source_html is None
            or not _valid_snapshot(snapshot)):
        return "", None
    return snapshot.source_html, snapshot.manifest.get("utility_geography")


def _published_road(
    session: Session, document: EvidenceDocument, run: EditorialRun, post: Post,
    streets: StreetIndex,
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
            incident = _road(document, notice, geometry, post.title, streets)
            if incident is not None:
                return incident
    # Старый deep link открывает геометрию уже опубликованной версии как архивную.
    lines = _road_lines(reviewed)
    return _incident(document, "road", post.title, (), archived=True, lines=tuple(lines),
                     source_map_url=point["notice_url"])


def export_map(
    session: Session, *, observed_at: datetime, streets: StreetIndex | None = None,
    areas: AreaIndex | None = None,
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
                       RawItem.url.contains("/elektricna-energija/"),
                       RawItem.url.contains("/beoinfo-vesti/") & (
                           RawItem.title.ilike("%bez vode%") | RawItem.title.contains("без воде")
                       ), published)),
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
            incident = _road(document, document, geometry, None, streets or {})
            if incident is not None and incident.ends_at is not None and incident.ends_at < cutoff:
                continue
        else:
            html, geography = _source_html(session, item)
            point = build_utility_point(document, html, geography)
            schedule = extract_power_scopes(document)
            water_scopes = extract_water_outage_scopes(document)
            if schedule:
                incident = _power(document, schedule, point, title, areas)
            elif (water_scopes or (point and point["mode"] == UTILITY_MAP_MODE)
                  or reported_water_places(document) is not None):
                incident = _water(document, point, title, streets or {}, areas, water_scopes)
            elif title and run is not None and post is not None:
                incident = _published_road(session, document, run, post, streets or {})
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
    area_index: Path | None = None,
) -> None:
    streets = load_street_index(street_index) if street_index is not None else None
    areas = load_area_index(area_index) if area_index is not None else None
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            feed = export_map(session, observed_at=datetime.fromisoformat(observed_at),
                              streets=streets, areas=areas)
        write_map_feed(output, feed)
    finally:
        engine.dispose()
