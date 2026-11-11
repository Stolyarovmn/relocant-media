"""Изолированный прототип точечной карты выставки для редакционного ревью, без доставки."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from html import escape
from typing import Any

from bs4 import BeautifulSoup

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.source_locations import is_public_https_url

MAX_HTML_BYTES = 2_000_000
_NUMBER = re.compile(r"-?\d{1,3}(?:\.\d{1,15})?$")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _type_is(node: dict[str, Any], name: str) -> bool:
    value = node.get("@type")
    return value == name or isinstance(value, list) and name in value


def _coordinate(value: object, limit: int) -> tuple[float, str] | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    text = str(value)
    if not _NUMBER.fullmatch(text):
        return None
    number = Decimal(text)
    if not number.is_finite() or abs(number) > limit:
        return None
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return float(number), canonical


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous duplicate JSON-LD field")
        result[key] = value
    return result


def _events(html: str) -> list[tuple[dict[str, Any], str, str]]:
    """Только локальный JSON-LD, без раскрытия @id, загрузки контекстов и геокодирования."""
    found: list[tuple[dict[str, Any], str, str]] = []
    soup = BeautifulSoup(html, "html.parser")
    for index, script in enumerate(soup.find_all("script", type="application/ld+json")):
        text = script.get_text()
        try:
            data = json.loads(text, parse_float=Decimal, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            continue
        roots = data if isinstance(data, list) else [data]
        for root_index, root in enumerate(roots):
            if not isinstance(root, dict) or root.get("@context") not in (
                "https://schema.org", "https://schema.org/", "http://schema.org",
            ):
                continue
            graph = root.get("@graph", [root])
            if not isinstance(graph, list):
                continue
            for node_index, node in enumerate(graph):
                if isinstance(node, dict) and _type_is(node, "ExhibitionEvent"):
                    pointer = f"script[{index}]/root[{root_index}]/node[{node_index}]"
                    found.append((node, _sha(text), pointer))
    return found


def build_exhibition_map_review(
    document: EvidenceDocument,
    source_html: str,
    *,
    primary_source_url: str,
    exhibition_name: str,
    venue_name: str,
    city: str,
    country: str,
    zoom: int = 15,
) -> dict[str, Any]:
    """Возвращаем review-only карту либо объяснённый текстовый fallback.

    Вызывающий код получает primary_source_url из проверенного реестра первоисточников.
    Имя события и площадка должны уже присутствовать в доказательствах новости.
    """
    if isinstance(zoom, bool) or not isinstance(zoom, int) or not 0 <= zoom <= 24:
        raise ValueError("map zoom must be an integer between 0 and 24")
    parameters = {
        "primary_source_url": primary_source_url,
        "exhibition_name": exhibition_name,
        "venue_name": venue_name,
        "city": city,
        "country": country,
        "zoom": zoom,
    }
    packet: dict[str, Any] = {
        "version": 1,
        "mode": "text",
        "reason": "explicit_event_coordinate_missing",
        "parameters": parameters,
        "source_id": document.source_id,
        "source_url": document.url,
        "source_html_sha256": _sha(source_html),
        "document_sha256": _sha(
            EvidenceDocument.model_validate(document.model_dump()).model_dump_json()),
        "network_access_performed": False,
        "geocoding_performed": False,
        "publication_permitted": False,
        "requires_independent_reviews": ["facts", "utility"],
    }
    if not is_public_https_url(primary_source_url) or document.url != primary_source_url:
        return {**packet, "reason": "primary_source_mismatch"}
    if len(source_html.encode("utf-8")) > MAX_HTML_BYTES:
        return {**packet, "reason": "source_html_too_large"}
    names = (exhibition_name, venue_name, city, country)
    if any(not _normalized(value) or len(value) > 250 for value in names):
        return {**packet, "reason": "explicit_event_and_locality_required"}
    evidence = _normalized(document.title + "\n" + document.body)
    if _normalized(exhibition_name) not in evidence or _normalized(venue_name) not in evidence:
        return {**packet, "reason": "event_or_venue_missing_from_evidence"}
    matching = [
        (event, script_sha, pointer) for event, script_sha, pointer in _events(source_html)
        if event.get("url") == primary_source_url
        and _normalized(event.get("name")) == _normalized(exhibition_name)
    ]
    # Даже два разных набора координат одного события требуют ручного разрешения.
    if len(matching) != 1:
        return {**packet, "reason": "event_coordinate_missing_or_ambiguous"}
    event, script_sha, pointer = matching[0]
    location = event.get("location")
    if not isinstance(location, dict) or not _type_is(location, "Place"):
        return packet
    if _normalized(location.get("name")) != _normalized(venue_name):
        return {**packet, "reason": "event_venue_mismatch"}
    address = location.get("address")
    geo = location.get("geo")
    if not isinstance(address, dict) or not isinstance(geo, dict):
        return packet
    if (
        _normalized(address.get("addressLocality")) != _normalized(city)
        or _normalized(address.get("addressCountry")) != _normalized(country)
    ):
        return {**packet, "reason": "source_locality_mismatch"}
    if not _type_is(geo, "GeoCoordinates"):
        return packet
    latitude = _coordinate(geo.get("latitude"), 90)
    longitude = _coordinate(geo.get("longitude"), 180)
    if latitude is None or longitude is None:
        return {**packet, "reason": "invalid_or_missing_explicit_coordinate"}
    caption = f"Место выставки: {venue_name}. Координаты указаны первоисточником."
    return {
        **packet,
        "mode": "point_map_review",
        "reason": "explicit_source_event_location_geo_requires_reviews",
        "coordinate_basis": {
            "json_ld_sha256": script_sha,
            "event_path": pointer,
            "field_path": "location.geo",
            "latitude": latitude[1],
            "longitude": longitude[1],
        },
        "rich_block": {
            "type": "map",
            "location": {"latitude": latitude[0], "longitude": longitude[0]},
            "zoom": zoom,
        },
        "rich_html": (
            f'<figure><tg-map lat="{latitude[1]}" long="{longitude[1]}" zoom="{zoom}"/>'
            f"<figcaption>{escape(caption)}</figcaption></figure>"
        ),
        "caption": caption,
    }


def validate_exhibition_map_review(
    packet: dict[str, Any], document: EvidenceDocument, source_html: str,
) -> None:
    """Проверяем повторным извлечением, включая координаты, весь HTML и подпись."""
    parameters = packet.get("parameters")
    if not isinstance(parameters, dict):
        raise ValueError("map review parameters missing")
    try:
        expected = build_exhibition_map_review(document, source_html, **parameters)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid map review parameters") from error
    if packet != expected:
        raise ValueError("map review no longer matches exact source and evidence")
