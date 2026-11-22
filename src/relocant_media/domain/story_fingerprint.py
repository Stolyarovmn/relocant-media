"""Deterministic "same story" check between a new Russian draft and recent posts (#333).

Different sources (N1, RTS, the transit operator, the city) retell one change in
different words, so the near-verbatim repeat check does not catch them. Two narrow,
explainable fingerprints do: the same transit lines, dates and reported scope,
and the same match score with a shared team name. Anything less certain is not a
duplicate here; the editorial review still judges novelty.
"""
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Literal

from relocant_media.domain.event_facts import conflicting_facts, event_dates, location_anchors

STORY_WINDOW = timedelta(hours=72)

_DATE_DOT = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{4}|\d{2}))?(?![\d.])")
_DAY_RANGE = re.compile(r"\b(\d{1,2})\s+по\s+(?=\d{1,2}\s+[а-я])", re.IGNORECASE)
_ROUTE_LIST = re.compile(
    r"(?:№|маршрут\w*|автобус\w*|трамва\w*|троллейбус\w*|лини\w*)\s*"
    r"(№?[ \t]*\d{1,3}[a-zа-я]?\b(?:[ \t]*(?:,|и|/)[ \t]*№?[ \t]*\d{1,3}[a-zа-я]?\b)*)",
    re.IGNORECASE,
)
_ROUTE = re.compile(r"\d{1,3}[a-zа-я]?\b", re.IGNORECASE)
_SCORE = re.compile(r"(?<![\d:.])(\d{1,3})\s*[:–—-]\s*(\d{1,3})(?![\d:.])")
_SCORE_CONTEXT = re.compile(
    r"сч[её]т|победил|обыграл|проиграл|матч|игр[аеыу]|тур[аеу]?\b|финал|ничь", re.I)
# Proper names: quoted («Партизан») or capitalised mid-sentence, never a sentence start.
_QUOTED = re.compile(r"«([^»]{2,40})»")
_NAME = re.compile(r"(?<=[\w,:;)»] )([A-ZА-ЯЁ][\w-]{3,})")
_LATIN_LIKE = str.maketrans({"n": "н", "a": "а", "b": "б", "e": "е"})
_PLACE_NAME = r"([A-ZА-ЯЁČĆŠŽĐ][\w-]+(?:[ \t]+[A-ZА-ЯЁČĆŠŽĐ][\w-]+)*)"
_TRANSPORT_SCOPE = re.compile(
    r"\b(?i:на|по|от|до|из|между|через|ул\.|улиц\w*|бульвар\w*|район\w*|"
    r"сторон\w*|остановк\w*|участ\w*|объезд\w*)"
    r"[ \t]+[«„\"]?" + _PLACE_NAME
    + r"|" + _PLACE_NAME + r"[ \t]+(?i:улиц\w*|бульвар\w*|мост\w*)\b"
)
_SCOPE_CHANGE = re.compile(
    r"\b(?:продл\w*|сократ\w*|перенес\w*|перенёс\w*|расшир\w*|отмен\w*|"
    r"возобнов\w*|восстанов\w*|обратн\w*\s+направлен\w*)\b",
    re.IGNORECASE,
)


def _transport_facts_text(text: str, year: int) -> str:
    def full_date(match: re.Match[str]) -> str:
        day, month, stated_year = match.groups()
        parsed_year = year if stated_year is None else int(stated_year)
        if stated_year is not None and len(stated_year) == 2:
            parsed_year += (year // 100) * 100
        try:
            return date(parsed_year, int(month), int(day)).strftime("%d.%m.%Y")
        except ValueError:
            return match[0]

    # Номера маршрутов уже сравнены отдельно. «№82» не должно терять число
    # при сравнении с «линия 82» из-за преобразования знака № в «No».
    return _DATE_DOT.sub(full_date, _DAY_RANGE.sub(r"\1 до ", _ROUTE_LIST.sub(" ", text)))


def _transport_scope(text: str) -> frozenset[str]:
    return location_anchors(text) | frozenset(
        (first or second).casefold() for first, second in _TRANSPORT_SCOPE.findall(text)
    )


def _routes(text: str) -> frozenset[str]:
    routes: set[str] = set()
    for group in _ROUTE_LIST.findall(text):
        routes.update(route.lower().translate(_LATIN_LIKE) for route in _ROUTE.findall(group))
    return frozenset(routes)


def _scores(text: str) -> frozenset[tuple[int, int]]:
    if not _SCORE_CONTEXT.search(text):
        return frozenset()
    scores = set()
    for match in _SCORE.finditer(text):
        left, right = match.groups()
        # "19:30" is a clock time; scores are "2:1" or "103:73".
        if len(right) == 2 and int(left) <= 23 and int(right) <= 59:
            continue
        scores.add((int(left), int(right)))
    return frozenset(scores)


def _names(text: str) -> frozenset[str]:
    names = [quoted.split()[0] for quoted in _QUOTED.findall(text)] + _NAME.findall(text)
    # Six letters absorb Russian case endings: «Баварию» / «Баварии».
    return frozenset(name.casefold()[:6] for name in names)


def same_story(
    new_title: str, new_body: str, old_title: str, old_body: str, *, year: int | None = None,
) -> Literal["transport", "sport"] | None:
    """Return the fingerprint kind when both texts describe one story, else ``None``."""
    new, old = f"{new_title}\n{new_body}", f"{old_title}\n{old_body}"
    routes = _routes(new)
    if (routes and routes == _routes(old)
            and not (_SCOPE_CHANGE.search(new) or _SCOPE_CHANGE.search(old))):
        reference_year = year if year is not None else date.today().year
        new_normalized = _transport_facts_text(new, reference_year)
        old_normalized = _transport_facts_text(old, reference_year)
        if (event_dates(new_normalized, reference_year)
                and _transport_scope(new) == _transport_scope(old)
                and not conflicting_facts(
                    new_normalized, old_normalized, reference_year, reference_year)):
            return "transport"
    scores = _scores(new) & _scores(old)
    if scores and len(_names(new) & _names(old)) >= 1:
        return "sport"
    return None


def find_same_story(
    title: str, body: str,
    publications: Sequence[tuple[int, str, str, datetime | None]],
    *, now: datetime, window: timedelta = STORY_WINDOW,
    kind: Literal["transport", "sport"] | None = None,
) -> int | None:
    """Earliest-matching recent publication id that tells the same story."""
    for publication_id, old_title, old_body, published_at in publications:
        if published_at is not None and now - published_at > window:
            continue
        match = same_story(title, body, old_title, old_body, year=now.year)
        if match is not None and (kind is None or match == kind):
            return publication_id
    return None
