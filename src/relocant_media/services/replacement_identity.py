"""Связь двух анонсов через полное название и явно указанный сеанс."""

import re
from datetime import timedelta

from relocant_media.domain.dedup import EventCandidate, same_event
from relocant_media.domain.event_facts import event_dates
from relocant_media.services.primary_discovery import folded

_NAMED = re.compile(
    r'\b(?:izlozb\w*|koncert\w*|festival\w*|predstav\w*)\s*'
    r'["„“«](?P<name>[^"„“”«»]{16,180})["“”»]',
)
_START = re.compile(
    r"^[\s,]*(?:u (?:ponedeljak|utorak|sredu|cetvrtak|petak|subotu|nedelju)[\s,]+)?"
    r"(?P<date>\d{1,2}\.\s*(?:[a-z]+(?:\s+20\d{2}\.?)?|\d{1,2}\.\s*20\d{2}\.?)"
    r")[\s,]+u\s+(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?:sati|casova|h\b)",
)
_UPDATE = re.compile(r"\b(?:otkaz\w+|odloz\w+|pomer\w+)\b")


def _named_slots(candidate: EventCandidate) -> set[tuple[str, str, int, int]]:
    assert candidate.published_at is not None
    text = " ".join(folded(candidate.body[:2500]).split())
    if _UPDATE.search(text):
        return set()
    slots = set()
    for name in _NAMED.finditer(text):
        phrase = " ".join(re.findall(r"[a-z0-9]+", name["name"]))
        if sum(len(word) >= 4 for word in phrase.split()) < 4:
            continue
        start = _START.match(text[name.end():name.end() + 95])
        if start is None:
            continue
        days = event_dates(start["date"], candidate.published_at.year)
        hour, minute = int(start["hour"]), int(start["minute"] or 0)
        if len(days) == 1 and hour < 24 and minute < 60:
            slots.add((phrase, next(iter(days)), hour, minute))
    return slots


def matches_replacement_event(original: EventCandidate, alternative: EventCandidate) -> bool:
    if same_event(original, alternative):
        return True
    if (original.published_at is None or alternative.published_at is None
            or abs(original.published_at - alternative.published_at) > timedelta(days=7)):
        return False
    left, right = _named_slots(original), _named_slots(alternative)
    return len(left) == 1 and left == right
