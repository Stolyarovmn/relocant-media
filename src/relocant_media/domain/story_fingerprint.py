"""Deterministic "same story" check between a new Russian draft and recent posts (#333).

Different sources (N1, RTS, the transit operator, the city) retell one change in
different words, so the near-verbatim repeat check does not catch them. Two narrow,
explainable fingerprints do: the same set of transit lines with an overlapping date,
and the same match score with a shared team name. Anything less certain is not a
duplicate here; the editorial review still judges novelty.
"""
import re
from collections.abc import Sequence
from datetime import datetime, timedelta

STORY_WINDOW = timedelta(hours=72)

_MONTHS = ("январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август",
           "сентябр", "октябр", "ноябр", "декабр")
_DATE_WORD = re.compile(
    r"(?<!\d)(\d{1,2})(?:\s*[-–—]\s*\d{1,2})?\s+(" + "|".join(_MONTHS) + r")[а-я]*",
    re.IGNORECASE,
)
_DATE_DOT = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?(?![\d.])")
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


def _dates(text: str) -> frozenset[tuple[int, int]]:
    found = {(int(day), next(index for index, stem in enumerate(_MONTHS, 1)
                             if month.lower().startswith(stem)))
             for day, month in _DATE_WORD.findall(text)}
    found |= {(int(day), int(month)) for day, month in _DATE_DOT.findall(text)
              if 1 <= int(day) <= 31 and 1 <= int(month) <= 12}
    return frozenset(found)


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


def same_story(new_title: str, new_body: str, old_title: str, old_body: str) -> str | None:
    """Return the fingerprint kind when both texts describe one story, else ``None``."""
    new, old = f"{new_title}\n{new_body}", f"{old_title}\n{old_body}"
    routes = _routes(new)
    if routes and routes == _routes(old) and _dates(new) & _dates(old):
        return "transport"
    scores = _scores(new) & _scores(old)
    if scores and len(_names(new) & _names(old)) >= 1:
        return "sport"
    return None


def find_same_story(
    title: str, body: str,
    publications: Sequence[tuple[int, str, str, datetime | None]],
    *, now: datetime, window: timedelta = STORY_WINDOW,
) -> int | None:
    """Earliest-matching recent publication id that tells the same story."""
    for publication_id, old_title, old_body, published_at in publications:
        if published_at is not None and now - published_at > window:
            continue
        if same_story(title, body, old_title, old_body):
            return publication_id
    return None
