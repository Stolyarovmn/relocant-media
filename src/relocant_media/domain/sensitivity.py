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


PERSONAL_DETAIL_REASON = "sensitive_personal_detail_in_draft"

# The search is over: only the outcome is news, the person's details are not. A
# confirmed outcome in any source wins over an older search notice in another one.
_FOUND = re.compile(
    r"(?<![^\W\d_])(?<!не\s)(?<!ni\s)(?<!nije\s)(?<!није\s)(?<!još\s)(?:"
    r"найден|нашли|нашёл|нашел|нашлась|нашлись|обнаружен|отыскал|"
    r"поиск\w*\s+(?:заверш|прекращ|окончен)|розыск\w*\s+(?:прекращ|заверш)|"
    r"вернул(?:ся|ась|ись)\s+(?:домой|к\s+(?:семье|родным|родителям))|"
    r"pronađen|пронађен|vratio\s+se|vratila\s+se|vraćen[aio]?\s+porodic|"
    r"potrag\w*\s+(?:je\s+)?(?:završ|okonč)|потраг\w*\s+(?:је\s+)?(?:заврш|оконч))",
    re.IGNORECASE,
)

# Wording of a search still under way. A bare mention of a disappearance is not enough:
# a later report about the same person repeats it.
_ACTIVE_SEARCH = re.compile(
    r"(?<![^\W\d_])(?:ищ(?:ет|ут)|разыскива|помогите\s+найти|просят\s+помочь|"
    r"пока\s+не\s+(?:найден|нашли)|traga|трага|traži\s+se|тражи\s+се|"
    r"pronađi\s+me|пронађи\s+ме|nije\s+pronađen|није\s+пронађен)",
    re.IGNORECASE,
)

# Health details that need not say "diagnosis". Used only for the draft check below,
# so it widens neither the general sensitive-topic review nor ordinary health news.
_HEALTH_DETAIL = re.compile(
    r"(?<![^\W\d_])(?:" + "|".join(_SIGNALS["health"]) + r"|аутист|autist|"
    r"эпилеп|epilep|епилеп|диабет|dijabet|дијабет|инвалид|invalid|синдром|sindrom|"
    r"болезн|заболеван|bolest|болест|лекарств|lekov|леков|психиатр|psihijatr|"
    r"шизофрен|šizofren|депресс|depres|особенност\w*\s+развит|задержк\w*\s+развит|"
    r"posebn\w*\s+potreb|посебн\w*\s+потреб|ментальн|умственн)",
    re.IGNORECASE,
)


def personal_detail_reason(source_texts: Iterable[str], title: str, body: str) -> str | None:
    """Hold a draft that names health details of a minor or of a person no longer missing.

    Only an active search may need such a detail, and then the owner decides it under the
    ordinary sensitive-topic review. A hold here is not owner-approvable: the text must be
    rewritten without the detail. Sources alone never trigger it; the draft must carry it.
    A confirmed outcome in a source or the draft ends the search even when another source
    is the original notice.
    """
    sources = tuple(text for text in source_texts if text)
    draft_texts = tuple(text for text in (title, body) if text)
    texts = (*sources, *draft_texts)
    story = sensitive_signals(texts)
    if not story & {"minor", "missing"}:
        return None
    if any(_FOUND.search(text) for text in texts) and any(
            _HEALTH_DETAIL.search(text) for text in draft_texts):
        # The person is found: any health detail in the draft counts.
        return PERSONAL_DETAIL_REASON
    if "missing" in story or any(_ACTIVE_SEARCH.search(text) for text in texts):
        return None
    # Outside a search only a named diagnosis of a minor is held: child-health news
    # such as a flu season in schools is not about one child.
    if "health" in sensitive_signals(draft_texts):
        return PERSONAL_DETAIL_REASON
    return None
