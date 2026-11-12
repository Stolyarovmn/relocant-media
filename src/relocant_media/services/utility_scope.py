"""Точные адреса из датированного городского списка, без геокодирования и расширения зон."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services.utility_geography import _name as osm_name

_ARTICLE = re.compile(
    r"/(?:lat|cir)/servisne-informacije/elektricna-energija/a[1-9][0-9]*/[^/?#]+\.html\Z")
_MUNICIPALITIES = frozenset(name.casefold() for name in (
    "Barajevo", "Čukarica", "Grocka", "Lazarevac", "Mladenovac", "Novi Beograd",
    "Obrenovac", "Palilula", "Rakovica", "Savski venac", "Sopot", "Stari grad",
    "Surčin", "Voždovac", "Vračar", "Zemun", "Zvezdara",
    "Барајево", "Чукарица", "Гроцка", "Лазаревац", "Младеновац", "Нови Београд",
    "Обреновац", "Палилула", "Раковица", "Савски венац", "Сопот", "Стари град",
    "Сурчин", "Вождовац", "Врачар", "Земун", "Звездара",
))
_MONTHS = {}
for _number, _names in enumerate((
    "januar januara јануар јануара", "februar februara фебруар фебруара",
    "mart marta март марта", "april aprila април априла", "maj maja мај маја",
    "jun juna јун јуна", "jul jula јул јула", "avgust avgusta август августа",
    "septembar septembra септембар септембра", "oktobar oktobra октобар октобра",
    "novembar novembra новембар новембра", "decembar decembra децембар децембра",
), start=1):
    _MONTHS.update(dict.fromkeys(_names.split(), _number))
_TITLE = re.compile(
    r"(?:ELEKTRIČNA ENERGIJA|ЕЛЕКТРИЧНА ЕНЕРГИЈА):\s*"
    r"(?:Planirana isključenja|Планирана искључења)\s+(?:za|за)\s+"
    r"(?P<date>.+?)\s*(?:(?:godine|године)\.?)?\Z", re.IGNORECASE)
_WORDS_DATE = re.compile(r"([0-9]{1,2})\.\s+([^\W\d_]+)\s+([0-9]{4})\.?")
_NUMERIC_DATE = re.compile(r"([0-9]{1,2})\.\s*([0-9]{1,2})\.\s*([0-9]{4})\.?")
_ISO_DATE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_CLOCK = r"[0-9]{1,2}[.:][0-9]{2}"
_ROW = re.compile(
    rf"(?P<municipality>[^0-9:]{{2,60}}?)\s+(?P<start>{_CLOCK})\s*[-–—]\s*"
    rf"(?P<end>{_CLOCK})\s+(?P<addresses>.+)\Z")
_INTERVAL = re.compile(rf"{_CLOCK}\s*[-–—]\s*{_CLOCK}")
_SETTLEMENT = re.compile(r"(?:Naselje|Насеље)\s+([^:]+):\s*(.*)\Z", re.IGNORECASE)
_ENTIRE_SETTLEMENT = re.compile(
    r"(?:Naselje|Насеље)\s+([^():]+?)"
    r"(?:\s*\((?:zaseok|засеок)\s+([^():]+)\))?\.?\Z", re.IGNORECASE,
)
_HOUSE = r"[1-9][0-9]{0,4}[A-Za-zČĆŠŽĐčćšžđА-Яа-яЂЈЉЊЋЏђјљњћџ]?"
_NUMBER = re.compile(_HOUSE + r"\Z")
_RANGE = re.compile(rf"{_HOUSE}\s*[-–—]\s*{_HOUSE}\Z")
_NAME = re.compile(r"[\w .’'\-()/]+\Z", re.UNICODE)
MAX_BODY = 100_000
MAX_ADDRESSES = 300
_MAX_SCOPE_ITEMS = 1200
_SUFFIX_ORDER = "абвгдђежзијклљмнњопрстћуфхцчџш"


@dataclass(frozen=True)
class SettlementScope:
    municipality: str
    name: str
    parent: str | None = None


@dataclass(frozen=True)
class PowerScope:
    starts_at: datetime
    ends_at: datetime
    addresses: tuple[dict[str, str], ...]
    settlement: SettlementScope | None = None


def _official(document: EvidenceDocument) -> bool:
    if (document.source_id != "city_beograd_news" or document.published_at is None
            or document.published_at.tzinfo is None
            or any(ord(character) < 32 for character in document.url)):
        return False
    try:
        url = urlsplit(document.url)
    except ValueError:
        return False
    return (url.scheme == "https" and url.netloc in {"beograd.rs", "www.beograd.rs"}
            and not url.query and not url.fragment and _ARTICLE.fullmatch(url.path) is not None)


def power_outage_date(document: EvidenceDocument) -> date | None:
    """Дата отключения из заголовка; она никогда не становится датой публикации."""
    if not _official(document):
        return None
    title = _TITLE.fullmatch(" ".join(document.title.split()))
    if title is None:
        return None
    value = title["date"].strip()
    words, numeric, iso = (_WORDS_DATE.fullmatch(value), _NUMERIC_DATE.fullmatch(value),
                           _ISO_DATE.fullmatch(value))
    try:
        if words:
            return date(int(words[3]), _MONTHS[words[2].casefold()], int(words[1]))
        if numeric:
            return date(int(numeric[3]), int(numeric[2]), int(numeric[1]))
        if iso:
            return date(int(iso[1]), int(iso[2]), int(iso[3]))
    except (KeyError, ValueError):
        pass
    return None


def _minutes(clock: str) -> int:
    hour, minute = map(int, re.split(r"[.:]", clock))
    if not (0 <= minute < 60 and 0 <= hour <= 24) or hour == 24 and minute != 0:
        raise ValueError("Неверное время отключения")
    return hour * 60 + minute


def _name(value: str) -> bool:
    return (2 <= len(value) <= 120 and _NAME.fullmatch(value) is not None
            and any(character.isalpha() for character in value) and "_" not in value)


def _street_scopes(municipality: str, text: str) -> tuple[dict[str, str], ...]:
    scope = municipality
    street: str | None = None
    records = []
    for raw_token in text.split(","):
        token = raw_token.strip()
        if not token:
            continue
        settlement = _SETTLEMENT.fullmatch(token)
        if settlement:
            place, token = settlement[1].strip(), settlement[2].strip()
            if not _name(place):
                return ()
            scope = f"{municipality}, naselje {place}"
            street = None
        elif re.match(r"(?:Naselje|Насеље)\b", token, re.IGNORECASE):
            # Поселение без явной улицы/номера не превращается в адрес всего поселения.
            return ()
        if ":" in token:
            parts = token.split(":")
            if len(parts) != 2 or not _name(parts[0].strip()):
                return ()
            street, token = parts[0].strip(), parts[1].strip()
        if not token:
            continue
        if street is not None and (
            _NUMBER.fullmatch(token) or _RANGE.fullmatch(token) or token.casefold() in {"bb", "бб"}
        ):
            records.append({"municipality": scope, "street": street, "number": token})
        else:
            # Не переносим прежнюю улицу через непонятный адресный фрагмент.
            street = None
    return tuple(records)


def _house_order(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([1-9][0-9]{0,4})([а-яђјљњћџ]?)", osm_name(value))
    if not match or match[2] and match[2] not in _SUFFIX_ORDER:
        return None
    return int(match[1]), _SUFFIX_ORDER.index(match[2]) + 1 if match[2] else 0


def is_explicit_house(value: str) -> bool:
    return _NUMBER.fullmatch(value) is not None


def house_in_range(number: str, printed_range: str) -> bool:
    """Сопоставить существующий адрес с печатным диапазоном одной стороны улицы."""
    if not _RANGE.fullmatch(printed_range):
        return False
    lower, upper = (_house_order(value) for value in re.split(r"[-–—]", printed_range))
    candidate = _house_order(number)
    return (lower is not None and upper is not None and candidate is not None
            and lower <= candidate <= upper
            and lower[0] % 2 == upper[0] % 2 == candidate[0] % 2)


def extract_power_scopes(document: EvidenceDocument) -> list[PowerScope]:
    """Диапазоны, точные адреса и явно названные поселения в собственных окнах."""
    day = power_outage_date(document)
    if len(document.body) > MAX_BODY or day is None:
        return []
    midnight = datetime.combine(day, time.min, tzinfo=ZoneInfo("Europe/Belgrade"))
    scopes = []
    count = 0
    for line in document.body.splitlines():
        row = _ROW.fullmatch(" ".join(line.split()))
        if row is None or row["municipality"].casefold() not in _MUNICIPALITIES:
            continue
        try:
            start, end = _minutes(row["start"]), _minutes(row["end"])
        except ValueError:
            continue
        municipality, text = row["municipality"], row["addresses"]
        # Склеенные строки с другим интервалом нельзя приписать первому району.
        if start >= end or _INTERVAL.search(text):
            continue
        match = _ENTIRE_SETTLEMENT.fullmatch(text.rstrip(" ,"))
        settlement = None
        if match and _name(match[1]) and (not match[2] or _name(match[2])):
            settlement = SettlementScope(
                municipality, match[2] or match[1], match[1] if match[2] else None,
            )
        records = _street_scopes(municipality, text) if settlement is None else ()
        if records or settlement is not None:
            count += len(records) + int(settlement is not None)
            if count > _MAX_SCOPE_ITEMS:
                return []
            scopes.append(PowerScope(midnight + timedelta(minutes=start),
                                     midnight + timedelta(minutes=end), records, settlement))
    return scopes


def extract_power_addresses(document: EvidenceDocument) -> list[dict[str, str]]:
    """Только отдельные явно написанные дома; диапазоны, BB и догадки исключены.

    Каждая строка должна содержать собственные район и интервал. Поселение сохраняется
    в municipality после `, naselje `. Повтор одного адреса в разных интервалах остаётся
    повтором: этот разбор не объединяет отключения и не определяет время публикации.
    """
    return [address for _, _, addresses in extract_power_schedule(document)
            for address in addresses]


def extract_power_schedule(
    document: EvidenceDocument,
) -> list[tuple[datetime, datetime, list[dict[str, str]]]]:
    """Привязка точных домов к интервалу их строки, время Белграда."""
    schedule = []
    count = 0
    for scope in extract_power_scopes(document):
        records = [entry for entry in scope.addresses if is_explicit_house(entry["number"])]
        if not records:
            continue
        count += len(records)
        if count > MAX_ADDRESSES:
            return []
        schedule.append((scope.starts_at, scope.ends_at, records))
    return schedule
