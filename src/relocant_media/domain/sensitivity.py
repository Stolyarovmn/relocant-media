"""Deterministic tripwire for sensitive substance, independent of the model's category.

A ranker may label a missing teenager or a death as ``city``. Category alone must not
decide automatic publication, so any of these signals sends the story to a human.
The list is deliberately conservative: a false hold costs one owner tap, a false
auto-publication can harm a real person. It is not a verdict on the story itself.
"""
import re
from collections.abc import Iterable

SENSITIVE_TOPIC_REASON = "sensitive_topic_requires_human_review"

# Regex stems are matched at the start of a word, case-insensitively, in Serbian Latin,
# Serbian Cyrillic and Russian. Generic words like "deca"/"дети" are excluded on purpose:
# children's theatre and school holidays are ordinary city news.
_SIGNALS: dict[str, tuple[str, ...]] = {
    "minor": (
        "maloletn", "малолетн", "dečak", "dečač", "дечак", "devojčic", "девојчиц",
        "tinejdžer", "тинејџер", "несовершеннолетн", "подростк", "подросток",
        "школьник", "школьниц", "мальчик", "девочк",
    ),
    "missing": (
        # "nestala struja"/"nestanak vode" is an outage, not a person.
        r"nestao\b", r"нестао\b",
        r"nestal[aio]\s+(?:je\s+)?(?:osob|muškar|žen|devoj|mladić|starij|dečak|tinejdž)",
        r"нестал[аои]\s+(?:је\s+)?(?:особ|мушкар|жен|девој|младић|стариј|дечак|тинејџ)",
        r"pronađi\s+me", r"пронађи\s+ме", "пропавш", r"пропал[аи]?\s+без\s+вести",
        "розыск",
    ),
    "death": (
        "poginu", "погину", "preminu", "премину", "umro", "umrla", "умро", "умрла",
        r"smrtno\s+strada", r"смртно\s+страда", "beživotn", "беживотн", "погиб", "скончал",
        r"умер(?:ла|ли)?\b", r"тело\s+(?:найден|обнаруж)", r"найден[аоы]?\s+мёртв",
        r"найден[аоы]?\s+мертв", r"обнаружен[аоы]?\s+тело", r"найдено\s+тело",
    ),
    "violence": (
        "ubist", "убист", "ubijen", "убијен", "ubio", "убио", "izboden", "избоден",
        "pretučen", "претучен", "silovan", "силован", "nasilj", "насиљ", "otmic", "отмиц",
        "убийств", "убит", "избит", "избил", "изнасил", "насили", "похищ", "ножев", "зарезал",
    ),
    "health": (
        "autiz", "аутиз", "dijagnoz", "дијагноз", "диагноз", r"boluje\s+od", r"болује\s+од",
        "страдающ", "психическ",
    ),
    "suicide": (
        "samoubist", "самоубист", "suicid", "суицид", "самоубийств",
    ),
}

_AGE = re.compile(
    r"(?<!\d)(\d{1,2})\s*-?\s*(?:godišnj|годишњ|летн|лет\b|года\b|godina\b|година\b)",
    re.IGNORECASE,
)
_PATTERNS = {
    kind: re.compile(r"(?<![^\W\d_])(?:" + "|".join(stems) + ")",
                     re.IGNORECASE)
    for kind, stems in _SIGNALS.items()
}


def sensitive_signals(texts: Iterable[str]) -> frozenset[str]:
    """Return the kinds of sensitive substance present in any of ``texts``."""
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for kind, pattern in _PATTERNS.items():
            if pattern.search(text):
                found.add(kind)
        if any(int(match[1]) < 18 for match in _AGE.finditer(text)) and (
                found - {"minor"}):
            # An age under 18 matters only next to another signal: "12-летний юбилей"
            # of a festival or "16 godina tradicije" is not a person.
            found.add("minor")
    return frozenset(found)


def sensitive_topic_reason(texts: Iterable[str]) -> str | None:
    return SENSITIVE_TOPIC_REASON if sensitive_signals(texts) else None
