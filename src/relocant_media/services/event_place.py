"""Проверяемое место и время события; без сети, геокодирования и угаданных дат."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.city_profile import CityEditorialProfile
from relocant_media.domain.map_links import map_event_id
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.exhibition_map_point import (
    MAX_HTML_BYTES,
    _coordinate,
    _normalized,
    _type_is,
    _unique_object,
)
from relocant_media.services.media_review_packet import _valid_snapshot
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import RawItem, Source

_EVENT_TYPES = {
    "Event",
    "ExhibitionEvent",
    "MusicEvent",
    "TheaterEvent",
    "DanceEvent",
    "Festival",
    "ScreeningEvent",
    "SocialEvent",
    "EducationEvent",
    "LiteraryEvent",
    "FoodEvent",
    "SportsEvent",
    "VisualArtsEvent",
}
_CLOCK = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?$")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _base_document(document: EvidenceDocument) -> EvidenceDocument:
    return EvidenceDocument.model_validate(
        document.model_dump(include=set(EvidenceDocument.model_fields))
    )


def _same_public_host(article: str, registered: str) -> bool:
    try:
        url, source = urlsplit(article), urlsplit(registered)
        return bool(
            is_public_https_url(article)
            and url.hostname
            and source.hostname
            and url.hostname.removeprefix("www.") == source.hostname.removeprefix("www.")
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        )
    except ValueError:
        return False


def event_time(value: object, timezone: str) -> datetime | None:
    """ISO с явным часом; местное время только вне перехода летнего времени."""
    if not isinstance(value, str) or not _CLOCK.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        zone = ZoneInfo(timezone)
        if parsed.tzinfo is None:
            first, second = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                return None
            parsed = first
        local = parsed.astimezone(zone)
        if parsed.utcoffset() != local.utcoffset():
            return None
        if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local.replace(
            tzinfo=None
        ):
            return None
        return local
    except (ValueError, OverflowError):
        return None


def _event_nodes(html: str, url: str) -> list[dict[str, Any]]:
    found = []
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(
                script.get_text(), parse_float=Decimal, object_pairs_hook=_unique_object
            )
        except (ValueError, RecursionError):
            continue
        roots = value if isinstance(value, list) else [value]
        for root in roots:
            if not isinstance(root, dict) or root.get("@context") not in (
                "https://schema.org",
                "https://schema.org/",
                "http://schema.org",
            ):
                continue
            nodes = root.get("@graph", [root])
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if (
                    isinstance(node, dict)
                    and node.get("url") == url
                    and any(_type_is(node, kind) for kind in _EVENT_TYPES)
                ):
                    found.append(node)
    return found


def _json_ld_facts(
    document: EvidenceDocument, html: str, profile: CityEditorialProfile
) -> dict[str, Any] | None:
    nodes = _event_nodes(html, document.url)
    if len(nodes) != 1:
        return None
    node = nodes[0]
    if node.get("eventStatus") not in (None, "https://schema.org/EventScheduled"):
        return None
    if node.get("eventAttendanceMode") not in (
        None,
        "https://schema.org/OfflineEventAttendanceMode",
    ):
        return None
    location = node.get("location")
    if not isinstance(location, dict) or not _type_is(location, "Place"):
        return None
    address, geo = location.get("address"), location.get("geo")
    if (
        not isinstance(address, dict)
        or not isinstance(geo, dict)
        or not _type_is(geo, "GeoCoordinates")
    ):
        return None
    if _normalized(address.get("addressLocality")) not in {
        *profile.geography.city_aliases,
        profile.city_name,
    } or _normalized(address.get("addressCountry")) not in {
        *profile.geography.country_aliases,
        profile.country_name,
        profile.country_code,
    }:
        return None
    name, venue, street = (
        _normalized(value)
        for value in (
            node.get("name"),
            location.get("name"),
            address.get("streetAddress"),
        )
    )
    evidence = _normalized(document.title + " " + document.body)
    if (
        any(not value or len(value) > 260 for value in (name, venue, street))
        or name not in evidence
        or venue not in evidence
    ):
        return None
    latitude, longitude = (
        _coordinate(geo.get("latitude"), 90),
        _coordinate(geo.get("longitude"), 180),
    )
    if latitude is None or longitude is None:
        return None
    return {
        "event_name": name,
        "venue": {
            "name": venue,
            "address": street,
            "latitude": latitude[0],
            "longitude": longitude[0],
        },
        "starts_at": node.get("startDate"),
        "ends_at": node.get("endDate"),
        "time_basis": "source_event_json_ld",
        "coordinate_source_url": document.url,
        "venue_source_url": document.url,
    }


def resolve_event_place(
    document: EvidenceDocument,
    source_html: str,
    *,
    profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    """Caller проверяет официальный реестровый источник; результат подходит для двух ревью.

    expires_at — технический срок скрытия, не объявленное время конца. Неизвестный
    конец не заменяется предположением о длительности. При live-сверке разрешено
    исключать лишь source_html_sha256, остальные поля должны остаться неизменными.
    """
    document = _base_document(document)
    if (
        not is_public_https_url(document.url)
        or not isinstance(source_html, str)
        or len(source_html.encode()) > MAX_HTML_BYTES
    ):
        return None
    facts = _json_ld_facts(document, source_html, profile)
    if facts is None:
        from relocant_media.services.event_place_adapters import extract_event_adapter

        facts = extract_event_adapter(document, source_html, profile=profile)
    if facts is None:
        return None
    start = event_time(facts.get("starts_at"), profile.timezone)
    raw_end = facts.get("ends_at")
    end = event_time(raw_end, profile.timezone) if raw_end is not None else None
    if start is None or (raw_end is not None and end is None):
        return None
    if end is not None and (end <= start or end - start > timedelta(days=366)):
        return None
    expires = end or datetime.combine(
        start.date() + timedelta(days=1), time.min, ZoneInfo(profile.timezone)
    )
    return {
        **facts,
        "version": 1,
        "id": map_event_id(document.url),
        "source_id": document.source_id,
        "source_url": document.url,
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat() if end else None,
        "expires_at": expires.isoformat(),
        "timezone": profile.timezone,
        "source_html_sha256": _sha(source_html),
        "document_sha256": _sha(document.model_dump_json()),
    }


def resolve_saved_event_place(
    session: Session,
    document: EvidenceDocument,
    document_ids: list[int],
    *,
    profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    """Только точный официальный источник и валидный сохранённый HTML того же документа."""
    for raw_id in document_ids:
        raw = session.get(RawItem, raw_id)
        source = session.get(Source, raw.source_id) if raw else None
        snapshot = session.get(RawItemMediaSnapshot, raw_id)
        if (
            raw is None
            or source is None
            or source.ownership != "official"
            or not source.enabled
            or source.city != profile.city_id
            or raw.url != document.url
            or not _same_public_host(document.url, source.url)
            or raw.source_id != document.source_id
            or snapshot is None
            or snapshot.source_page != document.url
            or snapshot.source_html is None
            or not _valid_snapshot(snapshot)
            or EvidenceDocument.model_validate(raw, from_attributes=True)
            != _base_document(document)
        ):
            continue
        return resolve_event_place(document, snapshot.source_html, profile=profile)
    return None
