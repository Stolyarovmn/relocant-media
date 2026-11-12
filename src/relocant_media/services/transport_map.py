"""Проверяемая связь датированной новости с картой профильного первоисточника."""

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.event_facts import event_dates, street_anchors
from relocant_media.services.media_review_packet import _valid_snapshot
from relocant_media.services.transport_map_image import render_transport_map
from relocant_media.services.transport_map_source import extract_transport_map
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import RawItem, Source

SOURCE_ID = "bgprevoz_changes"
MAP_MODE = "transport_map_review"
MAX_NOTICES = 16
_SERBIAN = str.maketrans(dict(zip(
    "абвгдђежзијклљмнњопрстћуфхцчџш",
    ("a", "b", "v", "g", "d", "đ", "e", "ž", "z", "i", "j", "k", "l", "lj", "m", "n",
     "nj", "o", "p", "r", "s", "t", "ć", "u", "f", "h", "c", "č", "dž", "š"), strict=True,
)))
_ROUTE = r"\d{1,4}[a-zčćšžđ]?(?!\w)"
_ROUTES = re.compile(
    rf"\blinij\w*\s*(?:(?:broj|br\.)\s*)?({_ROUTE}(?:\s*(?:,|\bi\b)\s*{_ROUTE})*)",
    re.IGNORECASE,
)
_PLACE = re.compile(
    r"\b(?:okretnic\w*|stajališt\w*|terminus\w*)\s+(?:kbc\s+)?[„“\"]([^”\"“]{3,90})",
)
_STOP = re.compile(r"#\s*(\d{3,5})(?!\w)")


def _latin(value: str) -> str:
    # Preserve case for the established street-name extractor.
    return "".join(
        character.lower().translate(_SERBIAN).capitalize() if character.isupper()
        else character.translate(_SERBIAN)
        for character in value
    )


def _signature(title: str, body: str, year: int) -> tuple[
    frozenset[str], frozenset[str], frozenset[str], frozenset[str],
]:
    text = _latin(f"{title}\n{body}")
    places = {name.strip().casefold() for name in _PLACE.findall(text.casefold())}
    places.update(street_anchors(text))
    routes = {route.casefold() for match in _ROUTES.finditer(text)
              for route in re.findall(_ROUTE, match[1], re.I)}
    stops = frozenset(_STOP.findall(text))
    return event_dates(text, year), frozenset(routes), frozenset(places), stops


def is_transport_anchor(document: EvidenceDocument) -> bool:
    if document.source_id != "city_beograd_news" or document.published_at is None:
        return False
    dates, routes, places, stops = _signature(
        document.title, document.body, document.published_at.year,
    )
    return bool(dates and places and (routes or stops))


def matches_transport_notice(document: EvidenceDocument, notice: EvidenceDocument) -> bool:
    if notice.source_id != SOURCE_ID or not is_transport_anchor(document):
        return False
    assert document.published_at is not None
    year = document.published_at.year
    dates, routes, places, stops = _signature(document.title, document.body, year)
    official_dates, official_routes, official_places, official_stops = _signature(
        notice.title, notice.body, year,
    )
    return (dates == official_dates and routes == official_routes and stops == official_stops
            and bool(places & official_places))


def _document_digest(document: EvidenceDocument) -> str:
    base = EvidenceDocument.model_validate(document.model_dump())
    return hashlib.sha256(base.model_dump_json().encode()).hexdigest()


def transport_point(
    document: EvidenceDocument, notice: EvidenceDocument, html: str,
) -> dict[str, Any] | None:
    if not matches_transport_notice(document, notice):
        return None
    geometry = extract_transport_map(notice, html)
    if geometry is None:
        return None
    point = {
        "mode": MAP_MODE, "source_id": document.source_id, "source_url": document.url,
        "document_sha256": _document_digest(document),
        "notice_url": notice.url, "notice_sha256": _document_digest(notice),
        "transport_geometry": geometry,
    }
    if geometry["line"]:
        try:
            image = render_transport_map(geometry["line"])
        except ValueError:
            return None
        point["image_sha256"] = hashlib.sha256(image).hexdigest()
    return point


def _saved_point(
    session: Session, document: EvidenceDocument, raw: RawItem,
) -> dict[str, Any] | None:
    snapshot = session.get(RawItemMediaSnapshot, raw.id)
    if (snapshot is None or snapshot.source_page != raw.url or snapshot.source_html is None
            or not _valid_snapshot(snapshot)):
        return None
    notice = EvidenceDocument.model_validate(raw, from_attributes=True)
    return transport_point(document, notice, snapshot.source_html)


def build_transport_map(session: Session, document: EvidenceDocument) -> dict[str, Any] | None:
    if not is_transport_anchor(document):
        return None
    source = session.get(Source, SOURCE_ID)
    if (source is None or not source.enabled or source.ownership != "official"
            or source.city != "belgrade"):
        return None
    points = []
    for raw in session.scalars(select(RawItem).where(RawItem.source_id == SOURCE_ID)
                              .order_by(RawItem.ingested_at.desc(), RawItem.id.desc())
                              .limit(MAX_NOTICES * 2)):
        notice = EvidenceDocument.model_validate(raw, from_attributes=True)
        if matches_transport_notice(document, notice):
            points.append((raw, _saved_point(session, document, raw)))
    if len(points) != 1 or points[0][1] is None:
        return None
    return {"source_raw_item_id": points[0][0].id, "point": points[0][1]}


def verify_transport_map_snapshot(
    session: Session, native: dict[str, Any], document: EvidenceDocument,
) -> None:
    saved = build_transport_map(session, document)
    if (saved is None or type(native.get("source_raw_item_id")) is not int
            or native["source_raw_item_id"] != saved["source_raw_item_id"]
            or native["point"] != saved["point"]):
        raise ValueError("transport map does not match the saved official notice")
