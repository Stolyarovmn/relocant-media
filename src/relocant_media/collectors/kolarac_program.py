"""Текстовая программа, которую Коларац прикрепляет к карточке лекции."""

import re
from datetime import datetime
from io import BytesIO
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup
from pypdf import PdfReader, apply_configuration

MAX_PROGRAM_BYTES = 512_000
_MAX_TEXT_LENGTH = 15_000
_HOSTS = {"kolarac.rs", "www.kolarac.rs"}
_UPLOAD = re.compile(r"/wp-content/uploads/20\d{2}/(?:0[1-9]|1[0-2])/[^/\\]+\.pdf", re.I)
_DETAIL_LABELS = {"детаљније", "detaljnije"}
_CLOCK = r"[уu]\s+(?P<hour>\d{1,2})(?:[.:](?P<minute>\d{2}))?(?![\d.:])"
KOLARAC_SCHEDULE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\.\s*(?P<month>\d{1,2})\.\s*(?P<year>20\d{2})\.\s+"
    + _CLOCK, re.I,
)
_MONTHS = {
    form: number
    for number, names in enumerate((
        "januar januara јануар јануара", "februar februara фебруар фебруара",
        "mart marta март марта", "april aprila април априла", "maj maja мај маја",
        "jun juna јун јуна", "jul jula јул јула", "avgust avgusta август августа",
        "septembar septembra септембар септембра", "oktobar oktobra октобар октобра",
        "novembar novembra новембар новембра", "decembar decembra децембар децембра",
    ), 1)
    for form in names.split()
}
_PROGRAM_SCHEDULE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\.?\s+(?P<month>" + "|".join(_MONTHS)
    + r")(?:\s+(?P<year>20\d{2})\.?)?\s+" + _CLOCK, re.I,
)
_HALL = re.compile(
    r"\b(?:предавачк(?:и амфитеатар|ом амфитеатру)|predavačk(?:i amfiteatar|om amfiteatru)|"
    r"велик(?:а дворана|ој дворани)|velik(?:a dvorana|oj dvorani)|"
    r"мал(?:а сала|ој сали)|mal(?:a sala|oj sali))\s+"
    r"(?:коларчеве задужбине|kolarčeve zadužbine)\b", re.I,
)


def linked_program(html: str, page_url: str) -> tuple[str, datetime] | None:
    page = urlsplit(page_url)
    if page.scheme != "https" or page.hostname not in _HOSTS:
        return None
    if not page.path.startswith("/predavanja/"):
        return None
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.select("h1")
    if len(headings) != 1 or not (
        schedule := KOLARAC_SCHEDULE.fullmatch(headings[0].get_text(" ", strip=True))
    ):
        return None
    try:
        starts_at = _start(schedule, int(schedule["year"]), int(schedule["month"]))
    except ValueError:
        return None
    urls: set[str] = set()
    for node in soup.select(".entry-content a[href]"):
        if " ".join(node.get_text(" ", strip=True).split()).casefold() not in _DETAIL_LABELS:
            continue
        href = str(node["href"]).strip()
        if len(href) > 1000 or any(ord(char) < 32 for char in href):
            continue
        try:
            url = urljoin(page_url, href)
            target = urlsplit(url)
            if (target.scheme in {"http", "https"} and target.netloc in _HOSTS and not target.query
                    and not target.fragment and _UPLOAD.fullmatch(unquote(target.path))):
                # На сайте встречаются HTTP-ссылки на файлы, доступные через HTTPS.
                urls.add(target._replace(scheme="https").geturl())
        except ValueError:
            continue
    return (next(iter(urls)), starts_at) if len(urls) == 1 else None


def _start(match: re.Match[str], year: int, month: int) -> datetime:
    return datetime(year, month, int(match["day"]), int(match["hour"]),
                    int(match["minute"] or "0"))


def parse_linked_program(data: bytes, url: str, expected_start: datetime) -> tuple[str, str]:
    if not data.startswith(b"%PDF-") or len(data) > MAX_PROGRAM_BYTES:
        raise ValueError("invalid Kolarac program PDF")
    try:
        with apply_configuration(
            maximum_declared_stream_length=1_000_000, zlib_maximum_output_length=1_000_000,
            array_based_stream_maximum_output_length=1_000_000,
            stream_decoding_work_maximum_length=2_000_000, stream_parsing_work_maximum_count=80_000,
            page_tree_maximum_entries=100, page_tree_maximum_depth=12,
            xform_maximum_invocations_per_extraction=100, jbig2dec_binary=None,
        ):
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= 4:
                raise ValueError("unsupported PDF structure")
            text = "\n".join(page.extract_text() for page in reader.pages).strip()
    except Exception as error:
        raise ValueError("unreadable Kolarac program PDF") from error
    if not text or len(text) > _MAX_TEXT_LENGTH:
        raise ValueError("Kolarac program requires bounded selectable text")

    flattened = " ".join(text.split())
    matches = list(_PROGRAM_SCHEDULE.finditer(flattened))
    if len(matches) != 1:
        raise ValueError("Kolarac program schedule missing or ambiguous")
    match = matches[0]
    try:
        start = _start(match, int(match["year"] or expected_start.year),
                       _MONTHS[match["month"].casefold()])
    except ValueError as error:
        raise ValueError("invalid Kolarac program schedule") from error
    if start != expected_start:
        raise ValueError("Kolarac program schedule differs from the event page")
    hall = _HALL.search(flattened, match.end(), match.end() + 160)
    if hall is None:
        raise ValueError("Kolarac program has no explicit lecture hall")
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    title = lines[0]
    for line in lines[1:3]:
        if not title.isupper() or not line.isupper():
            break
        title += " " + line
    if not 8 <= len(title) <= 260 or not any(char.isalpha() for char in title):
        raise ValueError("Kolarac program title missing or invalid")
    schedule = f"Предавање ће се одржати {start:%d.%m.%Y.} у {start:%H:%M} · {hall[0]}"
    return title, f"{schedule}\n\n{text}\n\nПрограмма организатора (PDF) {url}"
