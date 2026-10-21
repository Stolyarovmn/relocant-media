"""Discover real Beograd.rs article media without inferring reuse rights."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

from relocant_media.collectors.beograd_article import (
    ArticleExtractionError,
    match_beograd_article_url,
)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_PATH = re.compile(r"/api/v3/images/(?P<image_id>\d+)/(?:medium|large|original)")
_IMAGE_ALT = re.compile(r"\.(?:jpe?g|png)$", re.I)
_ALLOWED_QUERY = {"ts"}


@dataclass(frozen=True, slots=True)
class BeogradMediaCandidate:
    url: str
    alt: str
    credit: str
    source_page: str
    rights_hint: str


@dataclass(frozen=True, slots=True)
class DownloadedBeogradMedia:
    path: Path
    sha256: str
    content_type: str
    size_bytes: int
    source_url: str


def _article_identity(url: str) -> tuple[str, str]:
    match = match_beograd_article_url(url)
    if match is None:
        raise ArticleExtractionError("unsupported Beograd article URL")
    parts = urlsplit(url)
    canonical = urlunsplit(("https", "www.beograd.rs", parts.path, "", ""))
    return canonical, match.group("article_id")


def _image_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if (
            parts.scheme != "https"
            or parts.hostname != "static.beograd.rs"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.fragment
            or IMAGE_PATH.fullmatch(parts.path) is None
            or any(key not in _ALLOWED_QUERY for key, _value in query)
        ):
            raise ValueError
    except ValueError as exc:
        raise ArticleExtractionError("unsupported Beograd media URL") from exc
    return urlunsplit(("https", "static.beograd.rs", parts.path, parts.query, ""))


def _credit_for(image: Tag) -> str:
    figure = image.find_parent("figure")
    if isinstance(figure, Tag):
        caption = figure.find("figcaption")
        if isinstance(caption, Tag):
            return " ".join(caption.stripped_strings)
    parent = image.parent
    if isinstance(parent, Tag):
        for sibling in parent.find_all_next(limit=3):
            if not isinstance(sibling, Tag):
                continue
            text = " ".join(sibling.stripped_strings)
            lowered = text.casefold()
            if lowered.startswith(("foto:", "фото:", "autor:", "аутор:")):
                return text
    return ""


def parse_beograd_media(html: str, article_url: str) -> list[BeogradMediaCandidate]:
    """Extract article-level static media while excluding UI icons and site logos."""
    canonical, _article_id = _article_identity(article_url)
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if not isinstance(h1, Tag):
        raise ArticleExtractionError("article h1 not found for media discovery")
    main = h1.find_parent("main") or soup

    candidates: dict[str, BeogradMediaCandidate] = {}
    for image in main.find_all("img"):
        if not isinstance(image, Tag):
            continue
        src = image.get("src")
        alt = image.get("alt", "")
        if not isinstance(src, str) or not isinstance(alt, str):
            continue
        try:
            url = _image_url(urljoin(canonical, src))
        except ArticleExtractionError:
            continue
        normalized_alt = " ".join(alt.split())
        # The current portal exposes story photos with file-like alt text; this
        # excludes typography controls and footer/brand images on the same page.
        if not _IMAGE_ALT.search(normalized_alt):
            continue
        credit = _credit_for(image)
        candidates[url] = BeogradMediaCandidate(
            url=url,
            alt=normalized_alt,
            credit=credit,
            source_page=canonical,
            rights_hint="explicit_credit_requires_review" if credit else "rights_unconfirmed",
        )
    return list(candidates.values())


def download_beograd_media(
    candidate: BeogradMediaCandidate,
    destination: Path,
    client: httpx.Client,
) -> DownloadedBeogradMedia:
    """Bounded official-host download; reuse permission remains a separate review."""
    _article_identity(candidate.source_page)
    url = _image_url(candidate.url)
    if client.follow_redirects:
        raise ArticleExtractionError("automatic redirects must be disabled")
    data = bytearray()
    for hop in range(4):
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location or hop == 3:
                    raise ArticleExtractionError("invalid or excessive media redirect")
                url = _image_url(urljoin(url, location))
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_IMAGE_BYTES):
                raise ArticleExtractionError("media exceeds size limit")
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise ArticleExtractionError("media exceeds size limit")
                data.extend(chunk)
            break
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, extension = "image/png", ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        content_type, extension = "image/jpeg", ".jpg"
    else:
        raise ArticleExtractionError("unsupported image signature")
    digest = hashlib.sha256(data).hexdigest()
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / (digest + extension)
    with path.open("xb") as handle:
        handle.write(data)
    return DownloadedBeogradMedia(path, digest, content_type, len(data), url)
