"""Подтверждённые текстом участки отключений и ссылки на карты из статей BVK."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services.utility_scope import SettlementScope, is_explicit_house

MAX_HTML_BYTES = 2_000_000
MAX_DOCUMENT_BYTES = 100_000
_BVK_HOSTS = {"bvk.rs", "www.bvk.rs"}
_CITY_HOSTS = {"beograd.rs", "www.beograd.rs"}
_CITY_PATH = re.compile(
    r"/(?:lat|cir)/servisne-informacije/vodovod-i-kanalizacija/a[1-9][0-9]*/[^/]+\.html\Z",
)
_CITY_NEWS_PATH = re.compile(r"/(?:lat|cir)/beoinfo-vesti/a[1-9][0-9]*/[^/]+\.html\Z")
_POST_PATH = re.compile(r"/[a-z0-9]+(?:-[a-z0-9]+)+/\Z")
_LETTERS = "A-Za-zČĆŽŠĐčćžšđА-Яа-яЈјЉљЊњЂђЋћЏџ"
_NAME = rf"[{_LETTERS}][{_LETTERS} '\u2019-]{{0,98}}[{_LETTERS}]"
_MUNICIPALITY = re.compile(
    rf"\b(?:u|na|у|на)\s+(?:opštini|општини)\s+({_NAME}?)(?=\s*[,.;]|\s+"
    r"(?:prouzrokovaće|проузроковаће)\b)", re.I,
)
_OUTAGE = r"(?:bez vode će ostati potrošači|без воде ће остати потрошачи)"
_FULL_SETTLEMENT = re.compile(
    rf"{_OUTAGE}\s+(?:u celom naselju|у целом насељу)\s+(?P<name>{_NAME})"
    r"(?=\s*[.;]|\s*$)", re.I,
)
_AREA_END = r"(?=\s*[.;]|\s+(?:i u|и у)\s+(?:ulici|улици)\b|\s*$)"
_AREA_NAME = rf"[{_LETTERS}][{_LETTERS} '\u2019-]{{0,98}}?[{_LETTERS}]"
_ENTIRE_AREAS = (
    re.compile(
        r"(?:bez vode će (?:ostati|biti)|без воде ће (?:остати|бити))\s+"
        r"(?:potrošači|потрошачи)\s+(?:u|у)\s+"
        r"(?:(?:celom|čitavom|комплетном|целом|читавом|kompletnom)\s+"
        r"(?:naselju|mikronaselju|stambenom naselju|насељу|микронасељу|стамбеном насељу)"
        r"|(?:celoj|čitavoj|целој|читавој)\s+(?:mesnoj zajednici|месној заједници))\s+"
        rf"(?P<name>{_AREA_NAME}){_AREA_END}", re.I,
    ),
    re.compile(
        r"(?:bez vode će (?:ostati|biti)|без воде ће (?:остати|бити))\s+"
        r"(?:svi potrošači|сви потрошачи)\s+(?:u|у)\s+"
        r"(?:naselju|mikronaselju|насељу|микронасељу)\s+"
        rf"(?P<name>{_AREA_NAME}){_AREA_END}", re.I,
    ),
    re.compile(
        r"(?:bez vode će (?:ostati|biti)|без воде ће (?:остати|бити))\s+"
        r"(?:celo|čitavo|kompletno|цело|читаво|комплетно)\s+"
        r"(?:naselje|mikronaselje|насеље|микронасеље)\s+"
        rf"(?P<name>{_AREA_NAME}){_AREA_END}", re.I,
    ),
)
_AREA_LIST = re.compile(
    rf"{_OUTAGE}\s+(?:u sledećim naseljima|у следећим насељима)\s*:\s*$", re.I,
)
_AREA_EXCEPTION = re.compile(r"\b(?:osim|izuzev|осим|изузев)\b", re.I)
_HOUSE_SUFFIX = (
    rf"\s+(?:u|у)\s+(?:ulici|улици)\s+(?P<street>{_NAME})\s*"
    r"(?:[,:(]\s*)?(?:(?:na|на)\s+)?(?:kućni broj(?:evi)?|кућни број(?:еви)?"
    r"|broj(?:evi|evima)?|број(?:еви|евима)?)\s+"
    r"(?P<numbers>[0-9][^().;]{0,160})\s*\)?(?=\s*[.;]|\s*$)"
)
_HOUSE_LIST = re.compile(rf"{_OUTAGE}{_HOUSE_SUFFIX}", re.I)
_HOUSE_WITH_AREA = re.compile(rf"\b(?:i|и){_HOUSE_SUFFIX}", re.I)
_SEGMENTS = (
    re.compile(
        rf"{_OUTAGE}\s+(?:u|у)\s+(?:ulici|улици)\s+(?P<street>{_NAME})"
        rf",?\s+(?:(?:na delu|на делу)\s+)?(?:od|од)\s+(?P<start>{_NAME})"
        rf"\s+(?:do|до)\s+(?P<end>{_NAME})(?=\s*[.;]|\s*$)", re.I,
    ),
    re.compile(
        rf"{_OUTAGE}\s+(?:u|у)\s+(?P<street>{_NAME})\s+(?:ulici|улици)"
        rf",?\s+(?:(?:na delu|на делу)\s+)?(?:od|од)\s+(?P<start>{_NAME})"
        rf"\s+(?:do|до)\s+(?P<end>{_NAME})(?=\s*[.;]|\s*$)", re.I,
    ),
)
_MONTHS = {
    "januara": 1, "јануара": 1, "februara": 2, "фебруара": 2,
    "marta": 3, "марта": 3, "aprila": 4, "априла": 4,
    "maja": 5, "маја": 5, "juna": 6, "јуна": 6,
    "jula": 7, "јула": 7, "avgusta": 8, "августа": 8,
    "septembra": 9, "септембра": 9, "oktobra": 10, "октобра": 10,
    "novembra": 11, "новембра": 11, "decembra": 12, "децембра": 12,
}
_DATE = re.compile(
    r"\b([0-9]{1,2})\.\s*([0-9]{1,2})\.\s*([0-9]{4})\.?(?![0-9])|"
    r"\b([0-9]{1,2})\.\s*(" + "|".join(_MONTHS) + r")\s+([0-9]{4})\.", re.I,
)
_WATER = re.compile(r"без воде|bez vode|снабдевањ\w*\s+водом|snabdevanj\w*\s+vodom", re.I)
_CLOCK_RANGE = re.compile(
    r"\b(?:od|од)\s+([0-9]{1,2})[.:]([0-9]{2})\s+(?:do|до)\s+"
    r"([0-9]{1,2})[.:]([0-9]{2})\b", re.I,
)
_HOUR_RANGE = re.compile(
    r"\b(?:od|од)\s+([0-9]{1,2})\s+(?:do|до)\s+([0-9]{1,2})\s+"
    r"(?:sata|сати|časova|часова)\b", re.I,
)
_NEWS_DATE = re.compile(
    r"\b(?:u|у)\s+"
    r"(ponedeljak|utorak|sredu|četvrtak|petak|subotu|nedelju|"
    r"понедељак|уторак|среду|четвртак|петак|суботу|недељу),?\s+"
    r"([0-9]{1,2})\.\s*(" + "|".join(_MONTHS) + r")(?=\s*,?\s*(?:od|од)\s)", re.I,
)
_WEEKDAYS = ("ponedeljak utorak sredu četvrtak petak subotu nedelju "
             "понедељак уторак среду четвртак петак суботу недељу").split()
_NEWS_WATER = re.compile(
    r"planiran\w*\s+radov\w*\s+na\s+vodovodnoj\s+mreži|"
    r"планиран\w*\s+радов\w*\s+на\s+водоводној\s+мрежи", re.I,
)
_CANCELLED = re.compile(
    r"\b(?:otkazan\w*|odložen\w*|obustavljen\w*|neće biti prekida|"
    r"отказан\w*|одложен\w*|обустављен\w*|неће бити прекида)\b", re.I,
)
_REPORTED_SCOPE = re.compile(
    rf"(?:potrošači|потрошачи)\s+(?:u|у)\s+(?:naseljima|насељима)\s+"
    rf"(?P<places>[{_LETTERS}, '\u2019-]{{3,500}}?)"
    rf"(?:,\s*|\s+)(?:kao i ulicama|као и улицама)\s+"
    rf"(?P<streets>[{_LETTERS}, '\u2019-]{{3,500}})(?=\s*[.;]|\s*$)", re.I,
)
_MAP_ASSET = re.compile(
    r"/wp-content/uploads/[0-9]{4}/(?:0[1-9]|1[0-2])/"
    r"[a-z0-9_-]*\bmapa(?:[-_][a-z0-9_-]+)?\.(?:webp|png|jpe?g)\Z", re.I,
)
_MAP_LABEL = re.compile(r"(?:^|[-_\s])(?:mapa|мапа|карта|map)(?:$|[-_\s])", re.I)


@dataclass(frozen=True)
class WaterSettlementScope:
    starts_at: datetime
    ends_at: datetime
    settlement: SettlementScope


@dataclass(frozen=True)
class WaterScope:
    starts_at: datetime
    ends_at: datetime
    municipality: str
    settlements: tuple[SettlementScope, ...]
    segments: tuple[dict[str, str], ...]
    addresses: tuple[dict[str, str], ...]


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _official_path(url: str, hosts: set[str]) -> str | None:
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "?" in url or "#" in url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc not in hosts:
        return None
    return parts.path


def _source_allowed(document: EvidenceDocument) -> bool:
    if document.source_id == "bvk_planned_works":
        return _official_path(document.url, _BVK_HOSTS) == "/planirani-radovi/"
    if document.source_id == "bvk_outage_news":
        return bool(_POST_PATH.fullmatch(_official_path(document.url, _BVK_HOSTS) or ""))
    if document.source_id in {"city_beograd", "city_beograd_news"}:
        path = _official_path(document.url, _CITY_HOSTS) or ""
        return bool(_CITY_PATH.fullmatch(path) or (
            _CITY_NEWS_PATH.fullmatch(path) and _NEWS_WATER.search(_normalized(document.body))
        ))
    return False


def _single_event_date(document: EvidenceDocument) -> date | None:
    matches = list(_DATE.finditer(document.body))
    if not matches and document.source_id == "bvk_planned_works":
        matches = list(_DATE.finditer(document.title))
    dates: set[date] = set()
    try:
        for match in matches:
            if match[1]:
                dates.add(date(int(match[3]), int(match[2]), int(match[1])))
            else:
                dates.add(date(int(match[6]), _MONTHS[match[5].lower()], int(match[4])))
    except ValueError:
        return None
    if dates:
        return next(iter(dates)) if len(dates) == 1 else None
    published = document.published_at
    if (published is None or published.utcoffset() is None
            or document.published_at_precision not in {"day", "instant"}):
        return None
    relative = list(_NEWS_DATE.finditer(_normalized(document.body)))
    if len(relative) != 1:
        return None
    weekday, number, month = relative[0].groups()
    posted = published.astimezone(ZoneInfo("Europe/Belgrade")).date()
    for offset in range(15):
        day = posted + timedelta(days=offset)
        if (day.day == int(number) and day.month == _MONTHS[month.casefold()]
                and day.weekday() == _WEEKDAYS.index(weekday.casefold()) % 7):
            return day
    return None


def water_outage_window(document: EvidenceDocument) -> tuple[datetime, datetime] | None:
    """Только один явно указанный день и интервал работ из того же объявления."""
    if not _source_allowed(document) or len(document.body.encode()) > MAX_DOCUMENT_BYTES:
        return None
    body = _normalized(document.body)
    day = _single_event_date(document)
    if _CANCELLED.search(body) or _CANCELLED.search(document.title):
        return None
    return _window(day, body) if _WATER.search(body) is not None else None


def reported_water_places(document: EvidenceDocument) -> tuple[str, tuple[str, ...], str] | None:
    """Указанные места без утверждения, что отключены все дома или вся улица."""
    if water_outage_window(document) is None:
        return None
    text = _normalized(document.body)
    match = _REPORTED_SCOPE.search(text)
    if match is None:
        return None
    municipalities = {_normalized(row[1]) for row in _MUNICIPALITY.finditer(text)}
    if len(municipalities) != 1:
        return None
    names = tuple(re.split(r"\s*,\s*|\s+(?:i|и)\s+", match["places"].strip()))
    if not names or len(names) > 20 or any(not re.fullmatch(_NAME, name) for name in names):
        return None
    return next(iter(municipalities)), names, match["streets"].strip()[:260]


def _window(day: date | None, text: str) -> tuple[datetime, datetime] | None:
    intervals = _CLOCK_RANGE.findall(text) + [
        (start, "00", end, "00") for start, end in _HOUR_RANGE.findall(text)
    ]
    if day is None or len(intervals) != 1:
        return None
    try:
        sh, sm, eh, em = map(int, intervals[0])
        zone = ZoneInfo("Europe/Belgrade")
        start = datetime.combine(day, time(sh, sm), tzinfo=zone)
        end = datetime.combine(day, time(eh, em), tzinfo=zone)
    except ValueError:
        return None
    return (start, end) if start < end else None


def _street_name(value: str) -> str | None:
    value = re.sub(r"^(?:ulice|улице)\s+|\s+(?:ulice|улице)$", "", value, flags=re.I).strip()
    if not re.fullmatch(_NAME, value):
        return None
    # Номер дома или неопределённое окончание нельзя превращать в пересечение.
    if re.search(r"\b(?:broj\w*|број\w*|okoln\w*|околн\w*|bb|бб)\b", value, re.I):
        return None
    return value


def extract_water_settlement(document: EvidenceDocument) -> tuple[str, str] | None:
    """Явно указанное целое поселение в датированном объявлении."""
    if water_outage_window(document) is None:
        return None
    text = _normalized(document.body)
    municipalities = {_normalized(match[1]) for match in _MUNICIPALITY.finditer(text)}
    if len(municipalities) != 1 or re.search(r"\b(?:osim|izuzev|осим|изузев)\b", text, re.I):
        return None
    names = {match["name"] for match in _FULL_SETTLEMENT.finditer(text)}
    if len(names) != 1:
        return None
    return next(iter(municipalities)), next(iter(names))


def _list_names(lines: list[str], headings: set[str]) -> tuple[str, ...]:
    names = []
    for line in lines:
        if _AREA_EXCEPTION.match(line):
            return ()
        # Заголовки разделов и следующий текст не являются продолжением списка.
        if ((line.isupper() and line.casefold() in headings)
                or not re.fullmatch(rf"{_NAME}(?:\s*\([^()]+\))?", line)
                or len(line.split()) > 5 or not line[0].isupper()):
            break
        # Например, «Barajevo (centar)» подтверждает только часть поселения.
        if "(" not in line:
            names.append(line)
    return tuple(names)


def _area_names(text: str, list_items: tuple[str, ...]) -> tuple[str, ...]:
    if _AREA_EXCEPTION.search(text):
        return ()
    names = [match["name"] for pattern in _ENTIRE_AREAS for match in pattern.finditer(text)
             if not re.search(r"\b(?:i|и|deo|део|delu|делу)\b", match["name"], re.I)]
    return tuple(dict.fromkeys((*names, *list_items)))


def _house_addresses(
    text: str, municipality: str, *, alongside_area: bool,
) -> tuple[dict[str, str], ...]:
    addresses: list[dict[str, str]] = []
    matches = list(_HOUSE_LIST.finditer(text))
    if alongside_area:
        matches.extend(_HOUSE_WITH_AREA.finditer(text))
    for match in matches:
        street = _street_name(match["street"])
        numbers = re.split(r"\s*,\s*|\s+(?:i|и)\s+", match["numbers"].strip())
        if street and all(is_explicit_house(number) for number in numbers):
            addresses.extend({"municipality": municipality, "street": street, "number": number}
                             for number in numbers)
    return tuple(addresses)


def _water_scope(
    municipality: str, period: tuple[datetime, datetime], text: str,
    names: tuple[str, ...],
) -> WaterScope:
    segments = _segments(text, municipality)
    if names:
        for pattern in _ENTIRE_AREAS:
            for match in pattern.finditer(text):
                tail = re.match(
                    r"\s+(?:i|и)\s+((?:u|у)\s+(?:ulici|улици)\s+.+)", text[match.end():],
                )
                if match["name"] in names and tail:
                    segments.extend(_segments(f"bez vode će ostati potrošači {tail[1]}",
                                              municipality))
    return WaterScope(*period, municipality,
                      tuple(SettlementScope(municipality, name) for name in names),
                      tuple(segments),
                      _house_addresses(text, municipality, alongside_area=bool(names)))


def extract_water_outage_scopes(document: EvidenceDocument) -> tuple[WaterScope, ...]:
    """География каждого однозначного муниципального окна официального уведомления."""
    if not _source_allowed(document) or len(document.body.encode()) > MAX_DOCUMENT_BYTES:
        return ()
    day = _single_event_date(document)
    if day is None or _CANCELLED.search(document.title + " " + document.body):
        return ()
    lines = [text for line in document.body.splitlines() if (text := _normalized(line))]
    headings = {_normalized(match[1]).casefold()
                for match in _MUNICIPALITY.finditer(_normalized(document.body))}
    scopes = []
    for index, line in enumerate(lines):
        municipalities = {_normalized(match[1]) for match in _MUNICIPALITY.finditer(line)}
        period = _window(day, line)
        if len(municipalities) != 1 or period is None or _WATER.search(line) is None:
            continue
        municipality = next(iter(municipalities))
        list_items = _list_names(lines[index + 1:], headings) if _AREA_LIST.search(line) else ()
        following = lines[index + 1] if index + 1 < len(lines) else ""
        names = () if _AREA_EXCEPTION.match(following) else _area_names(line, list_items)
        scope = _water_scope(municipality, period, line, names)
        if scope.settlements or scope.segments or scope.addresses:
            scopes.append(scope)
    if scopes:
        return tuple(scopes)
    text = _normalized(document.body)
    municipalities = {_normalized(match[1]) for match in _MUNICIPALITY.finditer(text)}
    period = water_outage_window(document)
    if len(municipalities) != 1 or period is None:
        return ()
    scope = _water_scope(next(iter(municipalities)), period, text, _area_names(text, ()))
    return (scope,) if scope.settlements or scope.segments or scope.addresses else ()


def extract_water_settlements(document: EvidenceDocument) -> tuple[WaterSettlementScope, ...]:
    return tuple(WaterSettlementScope(scope.starts_at, scope.ends_at, place)
                 for scope in extract_water_outage_scopes(document) for place in scope.settlements)


def extract_water_segments(document: EvidenceDocument) -> list[dict[str, str]]:
    """Только явно названные улица и два пересечения; не геометрия и не список домов.

    Падеж и алфавит источника сохраняются. Неоднозначные даты/муниципалитеты и
    общие списки районов отклоняются; здесь нет геокодирования или сетевых вызовов.
    """
    try:
        if not _source_allowed(document) or len(document.body.encode()) > MAX_DOCUMENT_BYTES:
            return []
        if not _single_event_date(document):
            return []
    except UnicodeError:
        return []
    text = _normalized(document.body)
    municipalities = {_normalized(match[1]) for match in _MUNICIPALITY.finditer(text)}
    if len(municipalities) != 1:
        return []
    return _segments(text, next(iter(municipalities)))


def _segments(text: str, municipality: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for pattern in _SEGMENTS:
        for match in pattern.finditer(text):
            street = _street_name(match["street"])
            start = _street_name(match["start"])
            end = _street_name(match["end"])
            if not street or not start or not end or start.casefold() == end.casefold():
                continue
            item = {"municipality": municipality, "street": street,
                    "start": start, "end": end}
            if item not in result:
                result.append(item)
    return result


def _visible_article_link(link: Tag, article: Tag) -> bool:
    belongs_to_article = False
    for node in (link, *link.parents):
        if node is article:
            belongs_to_article = True
        if not isinstance(node, Tag):
            continue
        if node.name in {"nav", "footer", "aside", "template", "noscript"}:
            return False
        if node.has_attr("hidden") or node.has_attr("inert"):
            return False
        if str(node.get("aria-hidden", "")).lower() == "true":
            return False
        classes = {str(value) for value in node.get_attribute_list("class")}
        if classes & {"hidden", "invisible", "banner", "sidebar", "related", "widget"}:
            return False
        if re.search(
            r"display\s*:\s*none|visibility\s*:\s*hidden", str(node.get("style", "")), re.I,
        ):
            return False
    return belongs_to_article


def extract_official_water_map(document: EvidenceDocument, source_html: str) -> str | None:
    """Ссылка на единственный явно названный растр карты в точной статье BVK.

    Проверенный формат: post 91008, article .blog-meta a.small-preview[title=ledine-mapa]
    с ImageObject и растровым href. Поддержаны также явные map-ссылки внутри .entry-content.
    Возвращаем существующий href; не скачиваем файл, не утверждаем права на публикацию,
    не принимаем общие городские карты, iframe и картинки из другой новости.
    """
    try:
        if document.source_id != "bvk_outage_news" or not _source_allowed(document):
            return None
        if (len(source_html.encode()) > MAX_HTML_BYTES or
                len(document.body.encode()) > MAX_DOCUMENT_BYTES):
            return None
        if len(document.body) < 80 or not _WATER.search(document.body):
            return None
        soup = BeautifulSoup(source_html, "html.parser")
        canonicals = soup.find_all("link", rel="canonical")
        if len(canonicals) != 1 or canonicals[0].get("href") != document.url:
            return None
        title = _normalized(document.title)
        body = _normalized(document.body)
        articles = [article for article in soup.find_all("article")
                    if body in _normalized(article.get_text(" ", strip=True))
                    and any(_normalized(node.get_text(" ", strip=True)) == title
                            for node in article.select("h1,h2,h3,blockquote.first-quote"))]
        if len(articles) != 1:
            return None
        article = articles[0]
        urls: set[str] = set()
        for link in article.select(".blog-meta a[href],.entry-content a[href]"):
            if (link.find_parent("article") is not article or
                    not _visible_article_link(link, article)):
                continue
            href = str(link.get("href", ""))
            path = _official_path(href, _BVK_HOSTS)
            label = str(link.get("title", "")) + " " + link.get_text(" ", strip=True)
            if not path or not _MAP_ASSET.fullmatch(path) or not _MAP_LABEL.search(label):
                continue
            if re.search(r"cister|цистер|logo|banner|baner|belgrade-mapa", path + label, re.I):
                continue
            if link.find_parent(class_="blog-meta") and (
                link.get("itemprop") != "image" or
                link.get("itemtype") != "https://schema.org/ImageObject"
            ):
                continue
            urls.add(href)
        return next(iter(urls)) if len(urls) == 1 else None
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return None
