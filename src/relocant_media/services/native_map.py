"""Карта из неизменного первоисточника и точный HTML, входящий в два редакционных ревью."""

import hashlib
from html import escape
from typing import Any

from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument, RussianDraft
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
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import RawItem, Source


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _point(native: dict[str, Any]) -> dict[str, Any]:
    point = native.get("point")
    if not isinstance(point, dict) or any(
        not isinstance(point.get(key), str)
        for key in ("source_url", "source_id", "document_sha256", "source_html_sha256")
    ):
        raise ValueError("native map point is missing or malformed")
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


def build_native_map(
    session: Session, document_ids: list[int], documents: tuple[EvidenceDocument, ...], *,
    source_url: str, profile: CityEditorialProfile,
) -> dict[str, Any] | None:
    document = next((doc for doc in documents if doc.url == source_url), None)
    if document is None:
        return None
    for raw_id in document_ids:
        try:
            snapshot = _snapshot(session, raw_id, document)
        except ValueError:
            continue
        assert snapshot.source_html is not None
        if snapshot.source_html_bytes > MAX_HTML_BYTES:
            return None
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
    paragraphs = [draft.title, *draft.body.split("\n\n")]
    text = "".join("<p>" + escape(part).replace("\n", "<br/>") + "</p>" for part in paragraphs)
    return (text + _map_fragment(point)
            + f'<p><a href="{escape(source_url, quote=True)}">Источник</a></p>')


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
    if (point["source_url"] != presentation.illustration.get("source_url")
            or point["parameters"].get("primary_source_url") != point["source_url"]):
        raise ValueError("native map must use the exact primary source")
    city, country = point["parameters"].get("city"), point["parameters"].get("country")
    if (not isinstance(city, str) or not isinstance(country, str)
            or city not in {*profile.geography.city_aliases, profile.city_name}
            or country not in {*profile.geography.country_aliases, profile.country_code,
                               profile.country_name}):
        raise ValueError("native map locality does not match reviewed city profile")
    document = next((doc for doc in documents if doc.url == point["source_url"]
                     and doc.source_id == point["source_id"]), None)
    if document is None or point["document_sha256"] != _digest(
        EvidenceDocument.model_validate(document.model_dump()).model_dump_json()
    ):
        raise ValueError("native map does not match evidence document")


def verify_native_map_snapshot(
    session: Session, native: dict[str, Any], documents: tuple[EvidenceDocument, ...],
    document_ids: list[int],
) -> None:
    raw_id = native.get("source_raw_item_id")
    if type(raw_id) is not int or raw_id not in document_ids:
        raise ValueError("native map source is not in reviewed event")
    point = _point(native)
    document = next((doc for doc in documents if doc.url == point["source_url"]), None)
    if document is None:
        raise ValueError("native map document missing")
    snapshot = _snapshot(session, raw_id, document)
    assert snapshot.source_html is not None
    validate_exhibition_map_review(point, document, snapshot.source_html)


def verify_live_native_map(
    native: dict[str, Any], document: EvidenceDocument, html: str | None,
) -> None:
    point = _point(native)
    if point["source_url"] != document.url:
        return
    actual = build_exhibition_map_review(document, html or "", **point["parameters"])
    # Динамический HTML допускается только при неизменной геометрии, названии и JSON-LD.
    keys = ("mode", "coordinate_basis", "rich_block", "rich_html", "caption", "document_sha256")
    if any(actual.get(key) != point.get(key) for key in keys):
        raise ValueError("native map changed in live source")
