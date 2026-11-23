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
_MINOR_PERSON = (
    r"подрост(?:ок|ка|ку|ком|ке)|мальчик(?:а|у|ом|е)?|девочк(?:а|и|у|ой|е)|"
    r"реб[её]н(?:ок|ка|ку|ком|ке)|школьник(?:а|у|ом|е)?|школьниц(?:а|ы|у|ей|е)|"
    r"несовершеннолетн(?:ий|яя|его|ей|ему|юю|им)|"
    r"dečak(?:a|u|om)?|дечак(?:а|у|ом)?|devojčic(?:a|e|i|u|om)|девојчиц(?:а|е|и|у|ом)|"
    r"maloletni(?:k|ka|ku|kom|ca|ce|ci|cu)|малолетни(?:к|ка|ку|ком|ца|це|ци|цу)"
)
_AGED_PERSON = (
    r"юнош\w*|пар(?:ень|ня|ню|нем)|пассажир\w*|пациент\w*|ученик\w*|учениц\w*|"
    r"пешеход\w*|спортсмен\w*|девушк\w*|человек\w*|"
    r"mladić\w*|младић\w*|učenik\w*|ученик\w*|putnik\w*|путник\w*"
)
_AGE_ADJECTIVE = r"(?P<age>[0-9]{1,2})\s*[-–‑]?\s*(?:летн|godišnj|годишњ)\w*"
_INDIVIDUAL_MINOR = re.compile(rf"(?<!\w)(?:{_MINOR_PERSON})\b", re.I)
_PERSON_AGE = re.compile(
    rf"(?<!\w){_AGE_ADJECTIVE}\s+(?:{_AGED_PERSON}|{_MINOR_PERSON})\b", re.I,
)
_ACTIVE_SEARCH = re.compile(
    r"(?<![^\W\d_])(?:"
    r"(?:ищ(?:ет|ут)|разыскива(?:ет|ют)|помогите\s+найти|traga\s+za|трага\s+за|"
    r"traži\s+se|тражи\s+се)\s+"
    rf"(?:{_AGE_ADJECTIVE}\s+)?(?:{_MINOR_PERSON}|{_AGED_PERSON}|мужчин\w*|женщин\w*)\b|"
    r"(?:поиск|розыск)\w*\s+продолжа\w*|potraga\s+traje|потрага\s+траје|"
    r"pronađi\s+me|пронађи\s+ме)",
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
_INDIVIDUAL_CARE = re.compile(
    r"(?<!\w)(?:назначил[аи]?|принимает|нуждается|болеет|страдает|"
    r"диагностировал[аи]?|у\s+(?:него|не[её]|пострадавш(?:его|ей))|"
    r"проходит\s+лечен\w*|болује\s+од|boluje\s+od)\b",
    re.IGNORECASE,
)


def _specific_minor(texts: Iterable[str]) -> bool:
    return any(_INDIVIDUAL_MINOR.search(text) or any(
        int(match["age"]) < 18 for match in _PERSON_AGE.finditer(text)
    ) for text in texts)


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
    if not any(_HEALTH_DETAIL.search(text) for text in draft_texts):
        return None
    story = sensitive_signals(texts)
    minor_in_draft = _specific_minor(draft_texts)
    minor_in_source = _specific_minor(sources)
    if not (minor_in_draft or minor_in_source or story & {"minor", "missing"}):
        return None
    if any(_FOUND.search(text) for text in texts):
        return PERSONAL_DETAIL_REASON
    if any(_ACTIVE_SEARCH.search(text) for text in texts):
        return None
    care_in_draft = any(_INDIVIDUAL_CARE.search(text) for text in draft_texts)
    if minor_in_draft or (minor_in_source and care_in_draft) or (
            "minor" in story and "health" in sensitive_signals(draft_texts)):
        return PERSONAL_DETAIL_REASON
    return None
