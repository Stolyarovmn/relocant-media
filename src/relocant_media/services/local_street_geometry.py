"""Привязка границ из объявлений к локальному графу улиц OpenStreetMap."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from relocant_media.services.utility_geography import (
    _cross_street_names,
    _municipality,
    _name,
    _segment_line,
    _valid_input,
)

StreetIndex = dict[str, dict[str, list[list[Any]]]]


def load_street_index(path: Path) -> StreetIndex:
    with gzip.open(path, "rb") as stream:
        content = stream.read(32_000_001)
    if len(content) > 32_000_000:
        raise ValueError("local street index too large")
    data = json.loads(content)
    if data.get("version") != 1 or not isinstance(data.get("municipalities"), dict):
        raise ValueError("unsupported local street index")
    index: StreetIndex = {}
    for municipality, roads in data["municipalities"].items():
        names: dict[str, list[list[Any]]] = {}
        for road in roads:
            names.setdefault(_name(road[1]), []).append(road)
        index[_municipality({"municipality": municipality})] = names
    return index


def local_street_line(
    segment: dict[str, str], index: StreetIndex,
) -> dict[str, Any] | None:
    if _valid_input(segment, segment=True) is None:
        return None
    roads = index.get(_municipality(segment), {})
    names = {_name(segment["street"])} | _cross_street_names(segment["start"]) | (
        _cross_street_names(segment["end"])
    )
    nodes: set[int] = set()
    elements: list[dict[str, Any]] = []
    for name in names:
        for way_id, label, coordinates in roads.get(name, []):
            ids = [node for node, _, _ in coordinates]
            nodes.update(ids)
            elements.append({
                "type": "way", "id": way_id, "tags": {"name": label, "highway": "road"},
                "nodes": ids,
                "geometry": [{"lat": lat, "lon": lon} for _, lat, lon in coordinates],
            })
    elements.extend({"type": "node", "id": node} for node in nodes)
    return _segment_line(segment, elements)
