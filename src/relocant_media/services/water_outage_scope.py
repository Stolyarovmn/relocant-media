"""Подтверждённые текстом участки отключений и ссылки на карты из статей BVK."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from relocant_media.ai.models import EvidenceDocument

MAX_HTML_BYTES = 2_000_000
MAX_DOCUMENT_BYTES = 100_000
_BVK_HOSTS = {"bvk.rs", "www.bvk.rs"}
_CITY_HOSTS = {"beograd.rs", "www.beograd.rs"}
_CITY_PATH = re.compile(
    r"/(?:lat|cir)/servisne-informacije/vodovod-i-kanalizacija/a[1-9][0-9]*/[^/]+\.html\Z",
)
_POST_PATH = re.compile(r"/[a-z0-9]+(?:-[a-z0-9]+)+/\Z")
_LETTERS = "A-Za-zČĆŽŠĐčćžšđА-Яа-яЈјЉљЊњЂђЋћЏџ"
_NAME = rf"[{_LETTERS}][{_LETTERS} '\u2019-]{{0,98}}[{_LETTERS}]"
_MUNICIPALITY = re.compile(
    rf"\b(?:u|na|у|на)\s+(?:opštini|општини)\s+({_NAME})(?=\s*[,.;])", re.I,
)
_OUTAGE = r"(?:bez vode će ostati potrošači|без воде ће остати потрошачи)"
_FULL_SETTLEMENT = re.compile(
    rf"{_OUTAGE}\s+(?:u celom naselju|у целом насељу)\s+(?P<name>{_NAME})"
    r"(?=\s*[.;]|\s*$)", re.I,
)
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
_MAP_ASSET = re.compile(
    r"/wp-content/uploads/[0-9]{4}/(?:0[1-9]|1[0-2])/"
    r"[a-z0-9_-]*\bmapa(?:[-_][a-z0-9_-]+)?\.(?:webp|png|jpe?g)\Z", re.I,
)
_MAP_LABEL = re.compile(r"(?:^|[-_\s])(?:mapa|мапа|карта|map)(?:$|[-_\s])", re.I)


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
        return bool(_CITY_PATH.fullmatch(_official_path(document.url, _CITY_HOSTS) or ""))
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
    return next(iter(dates)) if len(dates) == 1 else None


def water_outage_window(document: EvidenceDocument) -> tuple[datetime, datetime] | None:
    """Только один явно указанный день и интервал работ из того же объявления."""
    if not _source_allowed(document) or len(document.body.encode()) > MAX_DOCUMENT_BYTES:
        return None
    body = _normalized(document.body)
    day = _single_event_date(document)
    intervals = _CLOCK_RANGE.findall(body)
    if day is None or len(intervals) != 1 or _WATER.search(body) is None:
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
    result: list[dict[str, str]] = []
    for pattern in _SEGMENTS:
        for match in pattern.finditer(text):
            street = _street_name(match["street"])
            start = _street_name(match["start"])
            end = _street_name(match["end"])
            if not street or not start or not end or start.casefold() == end.casefold():
                continue
            item = {"municipality": next(iter(municipalities)), "street": street,
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
