"""Карты из источника и явно обозначенный поиск; без геокодирования и сети."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from relocant_media.ai.models import EvidenceDocument

_BAD_URL = re.compile(r"[\s\\\x00-\x1f\x7f]|%(?:00|0a|0d|5c)", re.I)
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", re.I)
_REDIRECT_KEYS = {"url", "redirect", "redirect_uri", "redirect_url", "continue", "next"}


def is_public_https_url(url: str) -> bool:
    """Проверка синтаксиса, без DNS и HTTP; IP и локальные имена запрещены."""
    if not isinstance(url, str) or not url or len(url) > 4000 or _BAD_URL.search(url):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or not _HOST.fullmatch(host)
            or "." not in host
            or any(not label for label in host.split("."))
            or host.endswith((".localhost", ".local", ".internal", ".lan", ".home"))
            or host.replace(".", "").isdigit()
        ):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
        return False
    except ValueError:
        return False


def map_provider(url: str) -> str | None:
    """Только прямые ссылки известных карт; редиректоры и произвольные хосты исключены."""
    if not is_public_https_url(url):
        return None
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(key.casefold() in _REDIRECT_KEYS or "://" in value for key, value in query):
        return None
    path = parts.path
    host = parts.hostname
    if host in {"www.google.com", "google.com", "maps.google.com"} and (
        path == "/maps" or path.startswith("/maps/") or (host == "maps.google.com" and path == "/")
    ):
        return "google_maps"
    if host == "maps.app.goo.gl" and re.fullmatch(r"/[A-Za-z0-9_-]+", path):
        return "google_maps"
    if host in {"yandex.com", "yandex.ru", "yandex.rs"} and (
        path == "/maps" or path.startswith("/maps/")
    ):
        return "yandex_maps"
    if host in {"openstreetmap.org", "www.openstreetmap.org"} and (
        path in {"/", "/search", "/directions"}
        or re.fullmatch(r"/(?:node|way|relation)/[0-9]+", path)
    ):
        return "openstreetmap"
    return None


@dataclass(frozen=True, slots=True)
class SourceMap:
    url: str
    provider: str
    source_page: str
    source_html_sha256: str
    source_quote: str
    label: str = "Карта из источника"
    kind: str = "source_map_link"
    location_verified: bool = False


def extract_source_maps(html: str, source_page: str) -> list[dict[str, Any]]:
    """Сохраняем только ссылки внутри статьи, не трактуя их как проверенную точку."""
    if not is_public_https_url(source_page):
        return []
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.find_all("article") or soup.find_all("main")
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for container in containers:
        for anchor in container.find_all("a", href=True):
            if anchor.find_parent(["nav", "footer", "aside", "header"]):
                continue
            url = anchor.get("href")
            if not isinstance(url, str) or url in seen:
                continue
            provider = map_provider(url)
            if provider is None:
                continue
            # Внешняя ссылка не раскрывается и не переадресовывается автоматически.
            parent = anchor.parent if isinstance(anchor.parent, Tag) else anchor
            quote = " ".join(parent.stripped_strings)[:800]
            found.append(asdict(SourceMap(url, provider, source_page, digest, quote)))
            seen.add(url)
    return found


def address_search_map(
    *, address: str, city: str, country: str, quote: str, document: EvidenceDocument,
) -> dict[str, Any]:
    """Строим поиск только по дословной адресной цитате с городом и страной."""
    values = (address, city, country)
    if (
        not is_public_https_url(document.url)
        or not quote.strip()
        or quote not in document.body
        or any(
            not value.strip() or value != value.strip() or value not in quote for value in values
        )
        or any(len(value) > 250 or any(ord(char) < 32 for char in value) for value in values)
    ):
        raise ValueError("address, city and country must be supported by an exact source quote")
    return {
        "url": "https://www.google.com/maps/search/?" + urlencode({
            "api": "1", "query": ", ".join(values),
        }),
        "provider": "google_maps",
        "kind": "address_search",
        "label": "Поиск адреса на карте",
        "location_verified": False,
        "source_page": document.url,
        "source_id": document.source_id,
        "source_quote": quote,
        "address": address,
        "city": city,
        "country": country,
    }
