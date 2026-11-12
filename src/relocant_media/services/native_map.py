"""Карта из неизменного первоисточника и точный HTML, входящий в два редакционных ревью."""

import hashlib
from datetime import datetime
from html import escape
from typing import Any

from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument, RussianDraft
from relocant_media.bot.formatting import (
    TELEGRAM_TEXT_LIMIT,
    editorial_html_parts,
    formatted_text_units,
)
from relocant_media.domain.city_profile import CityEditorialProfile
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.exhibition_map_point import (
    MAX_HTML_BYTES,
    _coordinate,
    _events,
    build_exhibition_map_review,
    validate_exhibition_map_review,
)
from relocant_media.services.media_review_packet import _valid_snapshot
from relocant_media.services.notice_banner import render_notice_banner
from relocant_media.services.transport_map import (
    MAP_MODE,
    build_transport_map,
    verify_transport_map_snapshot,
)
from relocant_media.services.utility_map import (
    UTILITY_MAP_MODE,
    build_utility_point,
    utility_document_sha256,
    utility_map_fragment,
)
from relocant_media.services.utility_scope import extract_power_scopes
from relocant_media.services.water_outage_scope import (
    extract_water_outage_scopes,
    extract_water_settlement,
    water_outage_window,
)
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import RawItem, Source

LEGACY_NOTICE_LAYOUT = "map_notice_v1"
NOTICE_LAYOUT = "map_notice_v2"
NOTICE_COVER = "tg://photo?id=notice_cover"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _point(native: dict[str, Any]) -> dict[str, Any]:
    point = native.get("point")
    required = ("source_url", "source_id", "document_sha256")
    if not isinstance(point, dict) or any(
        not isinstance(point.get(key), str) for key in required
    ):
        raise ValueError("native map point is missing or malformed")
    if (point.get("mode") not in {MAP_MODE, UTILITY_MAP_MODE}
            and not isinstance(point.get("source_html_sha256"), str)):
        raise ValueError("native map source HTML digest is missing")
    return point


def _snapshot(session: Session, raw_id: int, document: EvidenceDocument) -> RawItemMediaSnapshot:
    raw = session.get(RawItem, raw_id)
    source = session.get(Source, raw.source_id) if raw is not None else None
    snapshot = session.get(RawItemMediaSnapshot, raw_id)
    if (raw is None or source is None or source.ownership.strip().casefold() != "official"
            or not source.enabled or raw.source_id != document.source_id or raw.url != document.url
            or snapshot is None or snapshot.source_page != document.url
            or snapshot.source_html is None or not _valid_snapshot(snapshot)):
        raise ValueError("native map requires exact official source HTML snapshot")
    return snapshot


def _notice_layout(
    document: EvidenceDocument, point: dict[str, Any], *, layout: str = NOTICE_LAYOUT,
) -> dict[str, Any]:
    legacy = layout == LEGACY_NOTICE_LAYOUT
    windows: dict[tuple[datetime, datetime], list[str]] = {}
    areas: dict[tuple[datetime, datetime], list[str]] = {}
    if point["mode"] == MAP_MODE:
        kind = "road"
    elif power := extract_power_scopes(document, include_whole_areas=not legacy):
        kind = "power"
        for scope in power:
            period = (scope.starts_at, scope.ends_at)
            items = windows.setdefault(period, [])
            streets: dict[tuple[str, str], list[str]] = {}
            for item in scope.addresses:
                numbers = streets.setdefault((item["street"], item["municipality"]), [])
                numbers.append(item["number"])
            items.extend(f"{street}, {', '.join(dict.fromkeys(numbers))} · {municipality}"
                         for (street, municipality), numbers in streets.items())
            if scope.settlement is not None:
                place = scope.settlement
                label = f"{place.name}, {place.parent}" if place.parent else f"{place.name} целиком"
                destination = items if legacy else areas.setdefault(period, [])
                destination.append(f"{label} · {place.municipality}")
    else:
        kind = "water"
        scopes = () if legacy else extract_water_outage_scopes(document)
        if scopes:
            for water_scope in scopes:
                period = (water_scope.starts_at, water_scope.ends_at)
                items = windows.setdefault(period, [])
                items.extend(
                    f'{segment["street"]} от {segment["start"]} до {segment["end"]} · '
                    f'{segment["municipality"]}' for segment in water_scope.segments
                )
                items.extend(
                    f'{item["street"]}, {item["number"]} · {item["municipality"]}'
                    for item in water_scope.addresses
                )
                for place in water_scope.settlements:
                    label = (f"{place.name}, {place.parent}" if place.parent else
                             f"{place.name} целиком")
                    areas.setdefault(period, []).append(f"{label} · {place.municipality}")
        elif legacy_period := water_outage_window(document):
            items = windows[legacy_period] = [
                f'{segment["street"]} от {segment["start"]} до {segment["end"]} · '
                f'{segment["municipality"]}' for segment in point["segments"]
            ]
            if legacy:
                if settlement := extract_water_settlement(document):
                    items.append(f"{settlement[1]} целиком · {settlement[0]}")
    return {
        "layout": layout,
        **({"cover_kind": kind,
            "cover_sha256": hashlib.sha256(render_notice_banner(kind)).hexdigest()}
           if legacy else {}),
        "schedule": [
            {"when": f"{start:%d.%m.%Y} · {start:%H:%M}–{end:%H:%M}",
             "items": list(dict.fromkeys(windows.get((start, end), ()))),
             **({} if legacy else
                {"areas": list(dict.fromkeys(areas.get((start, end), ())))})}
            for start, end in sorted(windows.keys() | areas.keys())
        ],
    }


def build_native_map(
    session: Session, document_ids: list[int], documents: tuple[EvidenceDocument, ...], *,
    source_url: str, profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    document = next((doc for doc in documents if doc.url == source_url), None)
    if document is None:
        return None
    if profile.city_id == "belgrade":
        transport = build_transport_map(session, document)
        if transport is not None:
            return {**transport, **_notice_layout(document, transport["point"])}
    for raw_id in document_ids:
        try:
            snapshot = _snapshot(session, raw_id, document)
        except ValueError:
            continue
        assert snapshot.source_html is not None
        if snapshot.source_html_bytes > MAX_HTML_BYTES:
            return None
        if profile.city_id == "belgrade":
            utility = build_utility_point(
                document, snapshot.source_html, snapshot.manifest.get("utility_geography"),
            )
            if utility is not None:
                return {"source_raw_item_id": raw_id, "point": utility,
                        **_notice_layout(document, utility)}
        events = [event for event, _, _ in _events(snapshot.source_html)
                  if event.get("url") == source_url]
        if len(events) != 1:
            return None
        event = events[0]
        location = event.get("location")
        if not isinstance(location, dict) or not isinstance(location.get("address"), dict):
            return None
        address = location["address"]
        city, country = address.get("addressLocality"), address.get("addressCountry")
        if (not isinstance(city, str) or not isinstance(country, str)
                or city not in {*profile.geography.city_aliases, profile.city_name}
                or country not in {*profile.geography.country_aliases, profile.country_code,
                                   profile.country_name}):
            return None
        if not isinstance(event.get("name"), str) or not isinstance(location.get("name"), str):
            return None
        point = build_exhibition_map_review(
            document, snapshot.source_html, primary_source_url=source_url,
            exhibition_name=event["name"], venue_name=location["name"], city=city, country=country,
        )
        return {"source_raw_item_id": raw_id, "point": point} if (
            point["mode"] == "point_map_review") else None
    return None


def _map_fragment(point: dict[str, Any]) -> str:
    """Строго пересобираем разрешённый тег; сырой HTML источника никогда не отправляется."""
    try:
        basis = point["coordinate_basis"]
        latitude = _coordinate(basis["latitude"], 90)
        longitude = _coordinate(basis["longitude"], 180)
        zoom = point["parameters"]["zoom"]
        venue = point["parameters"]["venue_name"]
        if (latitude is None or longitude is None or type(zoom) is not int
                or not 0 <= zoom <= 24 or not isinstance(venue, str)
                or point["mode"] != "point_map_review"
                or point["publication_permitted"] is not False):
            raise ValueError("invalid native map data")
        expected_block = {"type": "map", "location": {
            "latitude": latitude[0], "longitude": longitude[0]}, "zoom": zoom}
        caption = f"Место выставки: {venue}. Координаты указаны первоисточником."
        fragment = (f'<figure><tg-map lat="{latitude[1]}" long="{longitude[1]}" zoom="{zoom}"/>'
                    f"<figcaption>{escape(caption)}</figcaption></figure>")
        if (point["rich_block"] != expected_block or point["rich_html"] != fragment
                or point["caption"] != caption):
            raise ValueError("native map markup does not match coordinates and caption")
        return fragment
    except (KeyError, TypeError) as error:
        raise ValueError("incomplete native map data") from error


def native_message_html(native_map: dict[str, Any], draft: RussianDraft) -> str:
    point = _point(native_map)
    source_url = point.get("source_url")
    if not isinstance(source_url, str) or not is_public_https_url(source_url):
        raise ValueError("native map source URL is unsafe")
    if native_map.get("layout") == NOTICE_LAYOUT:
        return _source_preview_notice_html(native_map, draft)
    if native_map.get("layout") == LEGACY_NOTICE_LAYOUT:
        return _legacy_notice_message_html(native_map, draft)
    paragraphs = [draft.title, *draft.body.split("\n\n")]
    text = "".join("<p>" + escape(part).replace("\n", "<br/>") + "</p>" for part in paragraphs)
    if point.get("mode") == MAP_MODE:
        fragment = _transport_fragment(point)
    elif point.get("mode") == UTILITY_MAP_MODE:
        fragment = utility_map_fragment(point)
    else:
        fragment = _map_fragment(point)
    return (text + fragment
            + f'<p><a href="{escape(source_url, quote=True)}">Источник</a></p>')


def _paragraphs(value: str) -> str:
    result = []
    for part in value.split("\n\n"):
        paragraph = "<p>" + escape(part).replace("\n", "<br/>") + "</p>"
        if part.startswith("«") and part.endswith("»"):
            paragraph = "<blockquote>" + paragraph + "</blockquote>"
        result.append(paragraph)
    return "".join(result)


def _legacy_notice_message_html(native: dict[str, Any], draft: RussianDraft) -> str:
    point = native["point"]
    parts = [f'<figure><img src="{NOTICE_COVER}"/></figure>', f"<h2>{escape(draft.title)}</h2>"]
    if point["mode"] == UTILITY_MAP_MODE:
        budget = 12_000
        groups = []
        sections = native["schedule"]
        for section in sections[:100]:
            groups.append(f'<p><code>{escape(section["when"])}</code> · время Белграда</p>')
            items = []
            for item in section["items"]:
                label = escape(item).replace(" целиком · ", " <u>целиком</u> · ")
                item_html = f"<li>{label}</li>"
                budget -= len(item_html.encode())
                if budget < 0:
                    break
                items.append(item_html)
            if items:
                groups.append("<ul>" + "".join(items) + "</ul>")
            if budget < 0:
                break
        if groups:
            if budget < 0 or len(sections) > 100:
                groups.append("<p>Остальные адреса перечислены в первоисточнике.</p>")
            parts.append("<details><summary>Адреса и время работ</summary>"
                         + "".join(groups) + "</details>")
        parts.append("<details><summary>Подробности объявления</summary>"
                     + _paragraphs(draft.body) + "</details>")
    else:
        parts.append(_paragraphs(draft.body))
    parts.append(f'<p><a href="{escape(point["source_url"], quote=True)}">Источник</a></p>')
    return "".join(parts)


def _notice_item(item: str) -> str:
    return escape(item).replace(" целиком · ", " <u>целиком</u> · ")


def _source_preview_notice_html(native: dict[str, Any], draft: RussianDraft) -> str:
    parts, footer = editorial_html_parts(
        draft.title, draft.body, native["point"]["source_url"], notice=True,
    )
    sections = native["schedule"]
    areas = [
        f'<code>{escape(section["when"])}</code>\n'
        + "\n".join(f"• {_notice_item(item)}" for item in section["areas"])
        for section in sections if section["areas"]
    ]
    if areas:
        parts.append("<b>Зоны отключения</b> · местное время\n" + "\n".join(areas))
    items = [
        [f'<b>{escape(section["when"])}</b>',
         *(f"• {escape(item)}" for item in section["items"])]
        for section in sections if section["items"]
    ]
    if items:
        heading = "<b>Адреса и время работ</b> · местное время"
        overflow = "Полный список адресов — в источнике."
        reserved = formatted_text_units(
            "\n\n".join([*parts, heading, overflow, *footer]),
        )
        budget = TELEGRAM_TEXT_LIMIT - reserved if reserved <= TELEGRAM_TEXT_LIMIT else 12_000
        rows = []
        for interval, *addresses in items:
            budget -= formatted_text_units(interval) + 2
            if budget < formatted_text_units(addresses[0]) + 1:
                rows.append(overflow)
                break
            rows.append(interval)
            for address in addresses:
                budget -= formatted_text_units(address) + 1
                if budget < 0:
                    rows.append(overflow)
                    break
                rows.append(address)
            if budget < 0:
                break
        if rows == [overflow]:
            parts.append(overflow)
        else:
            parts.append("<blockquote expandable>" + "\n".join([heading, *rows]) + "</blockquote>")
    return "\n\n".join([*parts, *footer])


def _transport_fragment(point: dict[str, Any]) -> str:
    notice_url = point.get("notice_url")
    geometry = point.get("transport_geometry")
    if (not isinstance(notice_url, str) or not is_public_https_url(notice_url)
            or not isinstance(geometry, dict) or set(geometry) != {"latitude", "longitude", "line"}
            or not isinstance(geometry["line"], list)):
        raise ValueError("transport map geometry or source is malformed")
    latitude = _coordinate(geometry["latitude"], 90)
    longitude = _coordinate(geometry["longitude"], 180)
    if latitude is None or longitude is None:
        raise ValueError("transport map requires valid coordinates")
    block = (f'<figure><tg-map lat="{latitude[1]}" long="{longitude[1]}" zoom="16"/>'
             '<figcaption>Место работ по данным транспортного секретариата.</figcaption>'
             '</figure>')
    if geometry["line"]:
        if not isinstance(point.get("image_sha256"), str):
            raise ValueError("transport map image digest missing")
        block += ('<figure><img src="tg://photo?id=transport_map"/>'
                  '<figcaption>Схема участка работ по данным первоисточника.</figcaption></figure>')
    elif "image_sha256" in point:
        raise ValueError("transport point cannot include a line image")
    return block + f'<p><a href="{escape(notice_url, quote=True)}">Карта Секретариата</a></p>'


def bind_native_map_draft(
    presentation: EditorialPresentation, draft: RussianDraft,
) -> EditorialPresentation:
    if presentation.native_map is None:
        return presentation
    native = dict(presentation.native_map)
    html = native_message_html(native, draft)
    native.update(message_html=html, message_html_sha256=_digest(html))
    return presentation.model_copy(update={"native_map": native})


def validate_native_map_draft(
    presentation: EditorialPresentation, draft: RussianDraft,
    documents: tuple[EvidenceDocument, ...],
    *, profile: CityEditorialProfile,
) -> None:
    native = presentation.native_map
    if native is None:
        return
    if bind_native_map_draft(presentation, draft) != presentation:
        raise ValueError("native map full HTML changed after review")
    point = _point(native)
    if point["source_url"] != presentation.illustration.get("source_url"):
        raise ValueError("native map must use the exact primary source")
    if point.get("mode") in {MAP_MODE, UTILITY_MAP_MODE}:
        if profile.city_id != "belgrade":
            raise ValueError("native map locality does not match reviewed city profile")
    else:
        parameters = point["parameters"]
        if parameters.get("primary_source_url") != point["source_url"]:
            raise ValueError("native map must use the exact primary source")
        city, country = parameters.get("city"), parameters.get("country")
        if (not isinstance(city, str) or not isinstance(country, str)
                or city not in {*profile.geography.city_aliases, profile.city_name}
                or country not in {*profile.geography.country_aliases, profile.country_code,
                                   profile.country_name}):
            raise ValueError("native map locality does not match reviewed city profile")
    document = next((doc for doc in documents if doc.url == point["source_url"]
                     and doc.source_id == point["source_id"]), None)
    if document is None:
        raise ValueError("native map evidence document is missing")
    digest = (utility_document_sha256(document) if point.get("mode") == UTILITY_MAP_MODE else
              _digest(EvidenceDocument.model_validate(document.model_dump()).model_dump_json()))
    if point["document_sha256"] != digest:
        raise ValueError("native map does not match evidence document")
    if "layout" in native:
        layout = native["layout"]
        if (layout not in {LEGACY_NOTICE_LAYOUT, NOTICE_LAYOUT}
                or point.get("mode") not in {MAP_MODE, UTILITY_MAP_MODE}
                or any(native.get(key) != value
                       for key, value in _notice_layout(document, point, layout=layout).items())):
            raise ValueError("notice layout does not match evidence document")


def verify_native_map_snapshot(
    session: Session, native: dict[str, Any], documents: tuple[EvidenceDocument, ...],
    document_ids: list[int],
) -> None:
    point = _point(native)
    document = next((doc for doc in documents if doc.url == point["source_url"]), None)
    if document is None:
        raise ValueError("native map document missing")
    if point.get("mode") == MAP_MODE:
        verify_transport_map_snapshot(session, native, document)
        return
    raw_id = native.get("source_raw_item_id")
    if type(raw_id) is not int or raw_id not in document_ids:
        raise ValueError("native map source is not in reviewed event")
    snapshot = _snapshot(session, raw_id, document)
    assert snapshot.source_html is not None
    if point.get("mode") == UTILITY_MAP_MODE:
        if build_utility_point(
            document, snapshot.source_html, snapshot.manifest.get("utility_geography"),
        ) != point:
            raise ValueError("utility map differs from the exact source snapshot")
        return
    validate_exhibition_map_review(point, document, snapshot.source_html)


def verify_live_native_map(
    native: dict[str, Any], document: EvidenceDocument, html: str | None,
) -> None:
    point = _point(native)
    if point["source_url"] != document.url or point.get("mode") == MAP_MODE:
        return
    if point.get("mode") == UTILITY_MAP_MODE:
        if build_utility_point(document, html or "", point.get("geography")) != point:
            raise ValueError("utility map changed in live source")
        return
    actual = build_exhibition_map_review(document, html or "", **point["parameters"])
    # Динамический HTML допускается только при неизменной геометрии, названии и JSON-LD.
    keys = ("mode", "coordinate_basis", "rich_block", "rich_html", "caption", "document_sha256")
    if any(actual.get(key) != point.get(key) for key in keys):
        raise ValueError("native map changed in live source")
