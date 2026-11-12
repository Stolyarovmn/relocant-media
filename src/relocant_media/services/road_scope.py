"""Ограничения движения и границы участков из объявлений секретариата Белграда."""

from __future__ import annotations

import re
from typing import Any

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services.local_street_geometry import StreetIndex, local_street_line
from relocant_media.services.transport_map_source import _notice_id
from relocant_media.services.utility_geography import _MUNICIPALITIES, _name

_MAX_NOTICE_BYTES = 100_000
_MAX_SEGMENTS = 3
_SENTENCE = re.compile(
    r"(?<!\d)(?<!\bул)(?<!\bбр)[.!?]\s+"
    r"|[.!?]\s+(?=током|радови|возила|приликом|привремено|линиј)",
)
_ROAD = (
    r"улиц(?:а|е|и|у|ом)|коловоз\w*|саобраћајниц\w*|саобраћајн\w*\s+трак\w*|"
    r"раскрсниц\w*|окретниц\w*|булевар\w*|(?:ауто[- ]?)?пут(?:а|у|ем|евима|еви)?|"
    r"мост\w*|деониц\w*"
)
_RESTRICTION = re.compile(
    rf"\b(?:затварањ\w*|затвара\b|заузећ\w*|ограничењ\w*|сужавањ\w*)"
    rf"[^.;:!]{{0,95}}?\b(?:{_ROAD})\b|"
    rf"\b(?:{_ROAD})\b[^.;:!]{{0,95}}?\bзатворен\w*\s+за\s+саобраћај\b|"
    rf"\b(?:{_ROAD})\b[^.;:!]{{0,120}}?\bограничењ\w*\s+кретања\s+возила\b|"
    rf"\bобустављен\w*\s+саобраћај\s+на\s+(?:{_ROAD})\b",
)
_WORK = re.compile(
    rf"\b(?:рад(?:ови|ова|овима)\s+(?:(?:се\s+изводе)\s+)?"
    rf"(?:на\s+(?:изградњ\w*|реконструкциј\w*|продужењ\w*|"
    rf"асфалтирањ\w*(?:\s+и\s+уређењ\w*)?|уређењ\w*|"
    rf"(?:редовном\s+)?одржавањ\w*)\s+(?:у\s+|на\s+)?"
    rf"|(?:у|на)\s+)|(?:изводиће\s+се|изводе\s+се)\s+радови\s+на\s+)"
    rf"(?:{_ROAD})\b",
)
_REVERSED_WORK = re.compile(
    rf"\b(?:изградњ\w*|реконструкциј\w*|асфалтирањ\w*|уређењ\w*|"
    rf"одржавањ\w*|уклапањ\w*|продужењ\w*)\s+(?:у\s+|на\s+)?(?:{_ROAD})\b|"
    r"\b(?:у улици|на коловозу)\s+[^.,;:!]{1,80}\s+"
    r"(?:се изводе|изводе се)\s+радови(?=\s*(?:[,.;:]|$))",
)
_PLACE = r"[а-яђјљњћџ][а-яђјљњћџ0-9 '\u2019-]{0,78}?"
_CROSS = r"(?:(?:зоне\s+)?раскрснице\s+(?:са|с)\s+(?:улицом\s+)?|улице\s+)?"
_BOUNDARIES = (
    rf"(?:на делу|на деоници|у делу|на потезу)\s+од\s+{_CROSS}(?P<start>{_PLACE})"
    rf"(?:\s*\([^()]{1,80}\))?\s+до\s+{_CROSS}(?P<end>{_PLACE})"
    r"(?=\s*(?:[,.;:!?()]|$)|\s+(?:у периоду|где|када|доћи ће)\b)"
)
_SEGMENT = re.compile(
    rf"(?:\bулиц(?:а|е|и|у)\s+(?P<street>{_PLACE})\s*"
    r"(?:\([^()]{1,80}\)\s*)?(?:[-–—]\s*(?:деоница|фаза)\s+\d+\s*)?|"
    rf"\bдржавн\w*\s+пут\w*\s+[^();]{{1,30}}\s*\(ул(?:ица)?\.?\s+"
    rf"(?P<road_name>{_PLACE})\)\s*),?\s*{_BOUNDARIES}",
)
_PREVIOUS_STREET = re.compile(
    rf"\b(?:у\s+улици|улице)\s+(?P<street>{_PLACE})"
    r"(?=\s*(?:[,(.;:]|$)|\s+(?:у периоду|доћи ће|ће бити)\b)",
)
_CONTINUED_SCOPE = re.compile(r"\bрадови се изводе на коловозу(?: и на тротоару)?\s*,?\s*"
                              + _BOUNDARIES)
_MUNICIPALITY = re.compile(r"\(\s*(?:(?:го|градска општина)\s+)?([^()]{2,30})\s*\)")
_PHASE = re.compile(r"\bфаза\s+(\d+[а-я]?)\b")
_ROUTE = re.compile(r"\b(?:саобраћаће|саобраћати|измењен\w*\s+дел\w*\s+трас\w*)\b")
_CROSS_ENDINGS = (
    ("ске", "ска"), ("чке", "чка"), ("ове", "ова"), ("еве", "ева"),
    ("ској", "ска"), ("чкој", "чка"), ("овој", "ова"), ("евој", "ева"),
)


def _sentences(document: EvidenceDocument) -> list[str]:
    if _notice_id(document) is None or len(document.body.encode()) > _MAX_NOTICE_BYTES:
        return []
    return _SENTENCE.split(_name(document.body))


def _road_clause(sentence: str) -> bool:
    return bool(
        _RESTRICTION.search(sentence) or _WORK.search(sentence) or _REVERSED_WORK.search(sentence)
    )


def is_road_restriction(document: EvidenceDocument) -> bool:
    """Изменения остановки, маршрута или расписания сами по себе не являются дорожными."""
    return any(_road_clause(sentence) for sentence in _sentences(document))


def _cross_name(value: str, roads: dict[str, list[list[Any]]]) -> str | None:
    choices = {value}
    for suffix, nominative in _CROSS_ENDINGS:
        if value.endswith(suffix):
            choices.add(value.removesuffix(suffix) + nominative)
    available = choices.intersection(roads)
    return next(iter(available)) if len(available) == 1 else None


def _resolve(
    names: dict[str, str], index: StreetIndex, municipalities: set[str],
) -> dict[str, Any] | None:
    resolved = []
    for municipality in sorted(municipalities):
        roads = index.get(municipality, {})
        street = names["street"]
        start = _cross_name(names["start"], roads)
        end = _cross_name(names["end"], roads)
        if street not in roads or start is None or end is None:
            continue
        segment = {
            "municipality": municipality,
            "street": roads[street][0][1],
            "start": roads[start][0][1],
            "end": roads[end][0][1],
        }
        if geometry := local_street_line(segment, index):
            resolved.append(geometry)
    return resolved[0] if len(resolved) == 1 else None


def _scope_candidates(document: EvidenceDocument) -> list[tuple[dict[str, str], set[str]]]:
    sentences = _sentences(document)
    # У нескольких фаз могут быть разные даты. Общий период объявления
    # недостаточен, чтобы показать их одновременно.
    if len({phase for sentence in sentences for phase in _PHASE.findall(sentence)}) > 1:
        return []
    candidates = []
    previous: tuple[str, set[str]] | None = None
    for sentence in sentences:
        if not _road_clause(sentence):
            previous = None
            continue
        municipalities = {name.strip() for name in _MUNICIPALITY.findall(sentence)}
        municipalities &= _MUNICIPALITIES
        if len(municipalities) > 1:
            previous = None
            continue
        route = _ROUTE.search(sentence)
        work_scope = sentence[:route.start()] if route else sentence
        for match in _SEGMENT.finditer(work_scope):
            candidates.append((
                {
                    "street": (match["street"] or match["road_name"]).strip(),
                    "start": match["start"].strip(), "end": match["end"].strip(),
                },
                municipalities,
            ))
        if previous is not None and (continuation := _CONTINUED_SCOPE.search(work_scope)):
            street, previous_municipalities = previous
            if not municipalities or not previous_municipalities or municipalities == (
                previous_municipalities
            ):
                candidates.append((
                    {"street": street, "start": continuation["start"].strip(),
                     "end": continuation["end"].strip()},
                    municipalities or previous_municipalities,
                ))
        streets = {street.strip() for street in _PREVIOUS_STREET.findall(work_scope)}
        previous = (next(iter(streets)), municipalities) if len(streets) == 1 else None
    return candidates


def resolve_road_segments(
    document: EvidenceDocument, index: StreetIndex,
) -> list[dict[str, Any]]:
    """Наносить только два явно названных конца и единственный связный участок улицы.

    Используется при отсутствии официальной полилинии. Объекты OSM подтверждают
    топологию, а факт работ и границы берутся из одного утверждения первоисточника.
    """
    result = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for names, municipalities in _scope_candidates(document):
        geometry = _resolve(names, index, municipalities or set(index))
        if geometry is None:
            continue
        path = tuple(tuple(pair) for pair in geometry["coordinates"])
        key = min(path, path[::-1])
        if key not in seen:
            seen.add(key)
            result.append(geometry)
    return result if len(result) <= _MAX_SEGMENTS else []
