"""Разметка карты по точным адресам или границам из уведомления об отключении."""

import hashlib
import re
from datetime import UTC
from html import escape
from typing import Any
from urllib.parse import urlencode

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.exhibition_map_point import _coordinate
from relocant_media.services.transport_map_image import render_transport_map
from relocant_media.services.utility_scope import extract_power_addresses, extract_power_scopes
from relocant_media.services.water_outage_scope import (
    extract_official_water_map,
    extract_water_outage_scopes,
    extract_water_segments,
    reported_water_places,
)

UTILITY_MAP_MODE = "utility_map_review"
BVK_NEWS_SOURCE = "bvk_outage_news"
ADDRESS_LIMIT = 8
SEGMENT_LIMIT = 3
OSM_ATTRIBUTION = "https://www.openstreetmap.org/copyright"
_OSM_OBJECT = re.compile(r"https://www\.openstreetmap\.org/(?:node|way)/[1-9][0-9]*\Z")


def utility_scope(document: EvidenceDocument) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    addresses = extract_power_addresses(document)
    return addresses[:ADDRESS_LIMIT], extract_water_segments(document)[:SEGMENT_LIMIT]


def utility_document_sha256(document: EvidenceDocument) -> str:
    normalized = EvidenceDocument.model_validate(document.model_dump())
    published_at = normalized.published_at
    if published_at is not None and published_at.tzinfo is not None:
        normalized = normalized.model_copy(update={"published_at": published_at.astimezone(UTC)})
    return hashlib.sha256(normalized.model_dump_json().encode()).hexdigest()


def _coordinate_pair(lat: Any, lng: Any) -> tuple[str, str] | None:
    latitude, longitude = _coordinate(lat, 90), _coordinate(lng, 180)
    if latitude is None or longitude is None:
        return None
    return latitude[1], longitude[1]


def _map_geography(
    geography: Any, addresses: list[dict[str, str]], segments: list[dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(geography, dict):
        return {"points": [], "lines": []}
    raw_points = geography.get("points")
    raw_lines = geography.get("lines")
    raw_points = raw_points if isinstance(raw_points, list) else []
    raw_lines = raw_lines if isinstance(raw_lines, list) else []
    points, lines = [], []
    for address in addresses:
        candidates = [p for p in raw_points if isinstance(p, dict)
                      and p.get("address") == address]
        if len(candidates) != 1:
            continue
        point = candidates[0]
        pair = _coordinate_pair(point.get("latitude"), point.get("longitude"))
        url = point.get("osm_url")
        if pair is not None and isinstance(url, str) and _OSM_OBJECT.fullmatch(url):
            points.append({"address": address, "latitude": pair[0], "longitude": pair[1],
                           "osm_url": url})
    for segment in segments:
        candidates = [line for line in raw_lines if isinstance(line, dict)
                      and line.get("segment") == segment]
        if len(candidates) != 1:
            continue
        line = candidates[0]
        coordinates, urls = line.get("coordinates"), line.get("osm_urls")
        if (not isinstance(coordinates, list) or not 2 <= len(coordinates) <= 1000
                or not isinstance(urls, list) or not 1 <= len(urls) <= 20
                or any(not isinstance(url, str) or not _OSM_OBJECT.fullmatch(url) for url in urls)):
            continue
        pairs = [_coordinate_pair(*pair) for pair in coordinates
                 if isinstance(pair, list) and len(pair) == 2]
        if len(pairs) == len(coordinates) and None not in pairs:
            lines.append({"segment": segment, "coordinates": [list(p) for p in pairs if p],
                          "osm_urls": urls})
    return {"points": points, "lines": lines}


def build_utility_point(
    document: EvidenceDocument, html: str, geography: Any = None,
) -> dict[str, Any] | None:
    addresses, segments = utility_scope(document)
    official = extract_official_water_map(document, html)
    if (not addresses and not segments and official is None
            and not extract_power_scopes(document) and not extract_water_outage_scopes(document)
            and reported_water_places(document) is None):
        return None
    verified = _map_geography(geography, addresses, segments)
    point = {
        "mode": UTILITY_MAP_MODE, "source_url": document.url, "source_id": document.source_id,
        "document_sha256": utility_document_sha256(document),
        "addresses": addresses, "segments": segments, "official_map_url": official,
        "geography": verified,
    }
    # A single legible scheme per post. Other locations stay accessible through map links.
    if verified["lines"] and official is None:
        try:
            image = render_transport_map(verified["lines"][0]["coordinates"])
        except ValueError:
            pass
        else:
            point["image_sha256"] = hashlib.sha256(image).hexdigest()
    return point


def _search(*values: str) -> str:
    return "https://www.openstreetmap.org/search?" + urlencode({"query": ", ".join(values)})


def _link(url: str, label: str) -> str:
    if not is_public_https_url(url):
        raise ValueError("unsafe utility map link")
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'


def _address_label(address: dict[str, str]) -> str:
    return f'{address["street"]} {address["number"]}, {address["municipality"]}'


def utility_map_fragment(point: dict[str, Any]) -> str:
    official = point.get("official_map_url")
    if official is not None:
        return f'<p>{_link(official, "Официальная карта водоканала")}</p>'
    geography = point["geography"]
    fragments: list[str] = []
    for found in geography["points"][:3]:
        pair = _coordinate_pair(found["latitude"], found["longitude"])
        if pair is None:
            raise ValueError("invalid utility address point")
        latitude, longitude = pair
        label = _address_label(found["address"])
        fragments.append(f'<figure><tg-map lat="{latitude}" long="{longitude}" zoom="18"/>'
                         f'<figcaption>{escape(label)}</figcaption></figure>')
    if point.get("image_sha256") is not None:
        line = geography["lines"][0]
        centre = line["coordinates"][len(line["coordinates"]) // 2]
        lat, lng = _coordinate_pair(*centre) or (None, None)
        if lat is None or lng is None:
            raise ValueError("invalid utility map line")
        fragments.append(f'<figure><tg-map lat="{lat}" long="{lng}" zoom="16"/>'
                         '<figcaption>Место отключения воды.</figcaption></figure>'
                         '<figure><img src="tg://photo?id=transport_map"/>'
                         '<figcaption>Участок улицы между указанными перекрёстками.</figcaption>'
                         '</figure>')
    if point["addresses"]:
        resolved = {_address_label(p["address"]): p["osm_url"] for p in geography["points"]}
        fragments.append("<p>Часть адресов из объявления. Полный список — в источнике.</p>")
        for address in point["addresses"]:
            label = _address_label(address)
            url = resolved.get(label) or _search(label, "Beograd")
            fragments.append(f'<p>{_link(url, label)}</p>')
    for segment in point["segments"]:
        street, municipality = segment["street"], segment["municipality"]
        fragments.append(f'<p>{_link(_search(street, municipality, "Beograd"), street)}. '
                         f'Участок от {escape(segment["start"])} до {escape(segment["end"])}.</p>')
    fragments.append(f'<p>{_link(OSM_ATTRIBUTION, "Картографические данные © OpenStreetMap")}</p>')
    return "".join(fragments)
