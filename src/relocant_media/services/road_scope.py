"""Ограничения движения и границы участков из объявлений секретариата Белграда."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.bgsaobracaj_notices import SOURCE_ID, match_bgsaobracaj_notice
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
_DATED_ROAD = re.compile(rf"\bу (?:улици|ул\.)\s+(?P<street>{_PLACE}),\s*")
_PRINTED_DATE = r"\d{1,2}\.\s*\d{1,2}\.\s*20\d{2}\.?"
_DATED_STRETCH = re.compile(
    rf"од\s+(?:ул(?:ице)?\.?\s+)?(?P<start>{_PLACE})"
    rf"\s+до\s+(?:ул(?:ице)?\.?\s+)?(?P<end>{_PLACE})\s*,\s*"
    r"(?:фаза\s+(?P<phase>\d+)\s*[,.]?\s*)?"
    rf"у периоду од\s+(?P<first>{_PRINTED_DATE})\s*(?:године\s+)?"
    rf"до\s+(?P<last>{_PRINTED_DATE})(?:\s+године)?(?!\d)",
)
_NEXT_STRETCH = re.compile(r"\s*[,;]?\s*и\s+(?=од\s)")
_SECRETARIAT_RESTRICTION = re.compile(
    r"\b(?:обустављен\w*|ограничен\w*)\s+саобраћај\b[^.!?;]{0,180}\Z"
)
_LIST_DATE = r"\d{1,2}\.\s*\d{1,2}\.(?:\s*20\d{2}\.?)?"
_LIST_PERIOD = (
    rf"у\s+(?:временском\s+)?периоду\s+од\s+(?P<first>{_LIST_DATE})"
    rf"\s+до\s+(?P<last>{_LIST_DATE})(?:\s+године)?"
)
_LIST_INTRO = re.compile(
    rf"\b(?:заузећ\w*|затварањ\w*|сужавањ\w*)\s+(?:дела\s+)?коловоза\s+"
    rf"у\s+(?:улици|ул\.)\s+(?P<street>{_PLACE})\s*,\s*"
    rf"(?:на територији (?:градске )?општине (?P<municipality>{_PLACE})\s*,\s*)?"
    rf"{_LIST_PERIOD}\s*,?\s+по фазама:\s*\Z"
)
_LIST_PHASE = re.compile(
    rf"фаза\s+(?P<phase>\d+)\s*:\s*заузеће\s+"
    rf"(?:(?:десне|леве|крајње десне|крајње леве)\s+)?"
    rf"(?:коловозне|саобраћајне)\s+траке\s+"
    rf"од\s+(?:ул(?:ице)?\.?\s+)?(?P<start>{_PLACE})"
    rf"\s+до\s+(?:ул(?:ице)?\.?\s+)?(?P<end>{_PLACE})\s*,?\s+"
    rf"{_LIST_PERIOD}\s*[.:;]?\s*\Z"
)
_SIDEWALK_STRETCH = re.compile(
    rf"\b(?:заузет|затворен)\s+(?:тротоар|део\s+тротоара)\s+"
    rf"у\s+(?:улици|ул\.)\s+(?P<street>{_PLACE})\s*,\s*"
    rf"од\s+(?:ул(?:ице)?\.?\s+)?(?P<start>{_PLACE})"
    rf"\s+до\s+(?:ул(?:ице)?\.?\s+)?(?P<end>{_PLACE})\s*,"
)
_MUNICIPAL_PERIOD = re.compile(
    rf"\bна територији (?:градске )?општине (?P<municipality>{_PLACE})\s*,\s*"
    rf"у\s+(?:временском\s+)?периоду\s+(?:од\s+)?(?P<first>{_PRINTED_DATE})"
    rf"(?:\s+године)?\s+до\s+(?P<last>{_PRINTED_DATE})(?:\s+године)?\s*[.]?\Z"
)
_TRAFFIC_CHANGE_STRETCH = re.compile(
    rf"\b(?:биће|је)\s+измењен\s+режим\s+саобраћаја\s+"
    rf"у\s+(?:улици|ул\.)\s+(?P<street>{_PLACE})\s*,\s*"
    rf"од\s+(?:ул(?:ице)?\.?\s+)?(?P<start>{_PLACE})"
    rf"\s+до\s+(?:ул(?:ице)?\.?\s+)?(?P<end>{_PLACE})\s*,\s*\Z",
)
_CALENDAR_DATE = re.compile(_PRINTED_DATE)
_TIMED_OR_PHASED = re.compile(
    r"\b(?:фаз[аеу]|фазама|свак\w+|часов[аи]?|сати)\b|\b\d{1,2}:\d{2}\b",
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
    if value.startswith("трга "):
        choices.add("трг " + value.removeprefix("трга "))
    for suffix, nominative in _CROSS_ENDINGS:
        if value.endswith(suffix):
            choices.add(value.removesuffix(suffix) + nominative)
    available = choices.intersection(roads)
    return next(iter(available)) if len(available) == 1 else None


def _resolve(
    names: dict[str, str], index: StreetIndex, municipalities: set[str], *,
    allow_disconnected_crossings: bool = False,
) -> dict[str, Any] | None:
    resolved = []
    for municipality in sorted(municipalities):
        roads = index.get(municipality, {})
        name = names["street"]
        street = name if name in roads else _cross_name(name, roads)
        start = _cross_name(names["start"], roads)
        end = _cross_name(names["end"], roads)
        if street is None or start is None or end is None:
            continue
        segment = {
            "municipality": municipality,
            "street": roads[street][0][1],
            "start": roads[start][0][1],
            "end": roads[end][0][1],
        }
        if geometry := local_street_line(
            segment, index, allow_disconnected_crossings=allow_disconnected_crossings,
        ):
            resolved.append(geometry)
    return resolved[0] if len(resolved) == 1 else None


def _printed_date(value: str) -> date:
    day, month, year = map(int, value.strip(".").split("."))
    return date(year, month, day)


def _listed_period(
    first: str, last: str, *, year: int | None = None,
) -> tuple[date, date] | None:
    start_parts = [int(part) for part in first.strip(".").split(".")]
    end_parts = [int(part) for part in last.strip(".").split(".")]
    if len(end_parts) == 3:
        year = end_parts[2]
    if year is None:
        return None
    start_year = start_parts[2] if len(start_parts) == 3 else year
    try:
        start = date(start_year, start_parts[1], start_parts[0])
        end = date(year, end_parts[1], end_parts[0])
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _listed_secretariat_stretches(
    body: str, index: StreetIndex,
) -> list[tuple[date, date, dict[str, Any]]]:
    # Сборщик сохраняет каждый пункт списка отдельным абзацем.
    blocks = [block for line in body.splitlines() if (block := _name(line.strip("\u200b")))]
    intros = [(position, match) for position, block in enumerate(blocks)
              if (match := _LIST_INTRO.search(block))]
    if len(intros) != 1:
        return []
    position, intro = intros[0]
    period = _listed_period(intro["first"], intro["last"])
    if period is None:
        return []
    municipality = intro["municipality"]
    if municipality is not None and municipality not in _MUNICIPALITIES:
        return []
    first, last = period
    common_year = first.year if first.year == last.year else None
    stretches: list[tuple[date, date, dict[str, Any]]] = []
    phases = []
    for block in blocks[position + 1:]:
        match = _LIST_PHASE.fullmatch(block)
        if match is None:
            break
        if len(stretches) == _MAX_SEGMENTS or match["phase"] in phases:
            return []
        dates = _listed_period(match["first"], match["last"], year=common_year)
        if dates is None or not first <= dates[0] <= dates[1] <= last:
            return []
        geometry = _resolve(
            {"street": intro["street"].strip(), "start": match["start"], "end": match["end"]},
            index, {municipality} if municipality else set(index),
        )
        if geometry is None:
            return []
        stretches.append((*dates, geometry))
        phases.append(match["phase"])
    return stretches if phases == _PHASE.findall(_name(body)) else []


def _secretariat_sidewalk(body: str, index: StreetIndex) -> list[tuple[date, date, dict[str, Any]]]:
    # Общий интервал применим, когда в объявлении ровно один календарный период.
    normalized = _name(body)
    if len(_CALENDAR_DATE.findall(normalized)) != 2 or _TIMED_OR_PHASED.search(normalized):
        return []
    for line in body.splitlines():
        paragraph = _name(line)
        period = _MUNICIPAL_PERIOD.search(paragraph)
        if period is None:
            continue
        municipality = period["municipality"]
        if municipality not in _MUNICIPALITIES:
            return []
        try:
            first, last = _printed_date(period["first"]), _printed_date(period["last"])
        except ValueError:
            return []
        if first > last:
            return []
        matches = list(_SIDEWALK_STRETCH.finditer(paragraph, 0, period.start()))
        if not 1 <= len(matches) <= _MAX_SEGMENTS:
            return []
        stretches = []
        for match in matches:
            geometry = _resolve(match.groupdict(), index, {municipality})
            if geometry is None:
                return []
            stretches.append((first, last, {**geometry, "scope": "sidewalk"}))
        return stretches
    return []


def _secretariat_traffic_change(
    body: str, index: StreetIndex,
) -> list[tuple[date, date, dict[str, Any]]]:
    normalized = _name(body)
    if len(_CALENDAR_DATE.findall(normalized)) != 2 or _TIMED_OR_PHASED.search(normalized):
        return []
    for line in body.splitlines():
        paragraph = _name(line)
        period = _MUNICIPAL_PERIOD.search(paragraph)
        if period is None or period["municipality"] not in _MUNICIPALITIES:
            continue
        stretch = _TRAFFIC_CHANGE_STRETCH.search(paragraph, 0, period.start())
        if stretch is None:
            continue
        try:
            first, last = _printed_date(period["first"]), _printed_date(period["last"])
        except ValueError:
            return []
        if first > last:
            return []
        # A same-name branch is irrelevant only when it cannot connect the named crossings.
        geometry = _resolve(stretch.groupdict(), index, {period["municipality"]},
                            allow_disconnected_crossings=True)
        if geometry is None:
            return []
        return [(first, last, {**geometry, "scope": "traffic_change"})]
    return []


def resolve_secretariat_stretches(
    document: EvidenceDocument, index: StreetIndex,
) -> list[tuple[date, date, dict[str, Any]]]:
    """Resolve only complete, explicitly dated stretches in an official street notice."""
    if (document.source_id != SOURCE_ID or not match_bgsaobracaj_notice(document.url)
            or len(document.body.encode()) > _MAX_NOTICE_BYTES):
        return []
    if listed := _listed_secretariat_stretches(document.body, index):
        return listed
    if sidewalk := _secretariat_sidewalk(document.body, index):
        return sidewalk
    if traffic_change := _secretariat_traffic_change(document.body, index):
        return traffic_change
    body = _name(document.body)
    road = _DATED_ROAD.search(body)
    if road is None or not _SECRETARIAT_RESTRICTION.search(body[:road.start()]):
        return []
    # Require one named road and the complete sequence of phases before joining streets.
    if _DATED_ROAD.search(body, road.end()) is not None:
        return []
    municipality = set(_MUNICIPALITY.findall(body)) & _MUNICIPALITIES
    if len(municipality) > 1:
        return []
    stretches: list[tuple[date, date, dict[str, Any]]] = []
    phases: list[str | None] = []
    offset = road.end()
    while True:
        match = _DATED_STRETCH.match(body, offset)
        if match is None or len(stretches) == _MAX_SEGMENTS:
            return []
        try:
            first, last = _printed_date(match["first"]), _printed_date(match["last"])
        except ValueError:
            return []
        if first > last:
            return []
        geometry = _resolve(
            {"street": road["street"], "start": match["start"], "end": match["end"]},
            index, municipality or set(index),
        )
        if geometry is None:
            return []
        stretches.append((first, last, geometry))
        phases.append(match["phase"])
        next_stretch = _NEXT_STRETCH.match(body, match.end())
        if next_stretch is None:
            break
        offset = next_stretch.end()
    printed_phases = _PHASE.findall(body)
    if ([phase for phase in phases if phase is not None] != printed_phases
            or len(stretches) > 1 and (None in phases or len(set(phases)) != len(phases))):
        return []
    return stretches


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
