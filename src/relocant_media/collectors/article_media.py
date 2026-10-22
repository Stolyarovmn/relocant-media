"""Extract real image candidates from an already-fetched authoritative source page."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Tag

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_DECLARED_WIDTH = 200
MIN_DECLARED_HEIGHT = 120
_MAX_REDIRECTS = 4
_NOISE_TOKEN = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:logo|icon|favicon|sprite|avatar|emoji|tracker|tracking|pixel|spacer|blank|loader)"
    r"(?:[^a-z0-9]|$)",
    re.I,
)
_CREDIT_PREFIX = re.compile(
    r"^(?:foto|photo|credit|autor|author|фото|аутор)\s*:|^©",
    re.I,
)

MediaOrigin = Literal["article_image", "og_image", "twitter_image"]
RightsHint = Literal["rights_unconfirmed", "explicit_attribution_requires_rights_review"]
EventMatch = Literal["unreviewed"]


class ArticleMediaError(ValueError):
    """The source page or an image candidate violates media safety constraints."""


@dataclass(frozen=True, slots=True)
class ArticleMediaCandidate:
    url: str
    source_page: str
    origin: MediaOrigin
    alt: str
    attribution: str
    provenance_basis: str
    rights_hint: RightsHint
    event_match: EventMatch = "unreviewed"
    declared_width: int | None = None
    declared_height: int | None = None


@dataclass(frozen=True, slots=True)
class DownloadedArticleMedia:
    path: Path
    sha256: str
    content_type: str
    size_bytes: int
    source_url: str


def _source_page(url: str) -> tuple[str, str]:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ArticleMediaError("invalid source page URL") from exc
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
    ):
        raise ArticleMediaError("unsupported source page URL")
    canonical = urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, ""))
    return canonical, parts.hostname.casefold().rstrip(".")


def _allowed_hosts(source_host: str, approved_image_hosts: frozenset[str]) -> frozenset[str]:
    values = {source_host}
    for host in approved_image_hosts:
        normalized = host.strip().casefold().rstrip(".")
        if not normalized or any(char in normalized for char in "/:@?#"):
            raise ArticleMediaError("invalid approved image host")
        values.add(normalized)
    return frozenset(values)


def _image_url(raw: str, source_page: str, allowed_hosts: frozenset[str]) -> str:
    candidate = urljoin(source_page, raw.strip())
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise ArticleMediaError("invalid image URL") from exc
    host = parts.hostname.casefold().rstrip(".") if parts.hostname else None
    if (
        parts.scheme != "https"
        or host not in allowed_hosts
        or parts.username is not None
        or parts.password is not None
        or port not in {None, 443}
        or not parts.path
    ):
        raise ArticleMediaError("unsupported image URL")
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def _text_attribute(tag: Tag, name: str) -> str:
    value = tag.get(name)
    return value.strip() if isinstance(value, str) else ""


def _dimension(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+)\s*(?:px)?\s*", value, re.I)
    return int(match.group(1)) if match else None


def _is_tiny(width: int | None, height: int | None) -> bool:
    if width is not None and width <= 32:
        return True
    if height is not None and height <= 32:
        return True
    return (
        width is not None
        and height is not None
        and (width < MIN_DECLARED_WIDTH or height < MIN_DECLARED_HEIGHT)
    )


def _looks_like_noise(tag: Tag | None, url: str, alt: str) -> bool:
    values = [url, alt]
    if tag is not None:
        values.extend((_text_attribute(tag, "id"), _text_attribute(tag, "role")))
        classes = tag.get("class")
        if isinstance(classes, list):
            values.extend(value for value in classes if isinstance(value, str))
    return any(_NOISE_TOKEN.search(value) for value in values if value)


def _explicit_attribution(image: Tag) -> str:
    figure = image.find_parent("figure")
    if not isinstance(figure, Tag):
        return ""
    caption = figure.find("figcaption")
    if not isinstance(caption, Tag):
        return ""
    text = " ".join(caption.stripped_strings)
    return text if _CREDIT_PREFIX.search(text) else ""


def _candidate(
    *,
    raw_url: str,
    source_page: str,
    allowed_hosts: frozenset[str],
    origin: MediaOrigin,
    provenance_basis: str,
    alt: str = "",
    attribution: str = "",
    width: int | None = None,
    height: int | None = None,
    tag: Tag | None = None,
) -> ArticleMediaCandidate | None:
    try:
        url = _image_url(raw_url, source_page, allowed_hosts)
    except ArticleMediaError:
        return None
    normalized_alt = " ".join(alt.split())
    if _is_tiny(width, height) or _looks_like_noise(tag, url, normalized_alt):
        return None
    rights_hint: RightsHint = (
        "explicit_attribution_requires_rights_review" if attribution else "rights_unconfirmed"
    )
    return ArticleMediaCandidate(
        url=url,
        source_page=source_page,
        origin=origin,
        alt=normalized_alt,
        attribution=attribution,
        provenance_basis=provenance_basis,
        rights_hint=rights_hint,
        declared_width=width,
        declared_height=height,
    )


def extract_article_media_candidates(
    html: str,
    source_page: str,
    *,
    approved_image_hosts: frozenset[str] = frozenset(),
) -> list[ArticleMediaCandidate]:
    """Extract source-related candidates without granting rights or event matching."""
    canonical, source_host = _source_page(source_page)
    allowed_hosts = _allowed_hosts(source_host, approved_image_hosts)
    soup = BeautifulSoup(html, "html.parser")
    candidates: dict[str, ArticleMediaCandidate] = {}

    article_roots = [tag for tag in soup.find_all("article") if isinstance(tag, Tag)]
    if not article_roots:
        main = soup.find("main")
        if isinstance(main, Tag):
            article_roots = [main]

    for root in article_roots:
        for image in root.find_all("img"):
            if not isinstance(image, Tag):
                continue
            raw_url = next(
                (
                    value
                    for name in ("src", "data-src", "data-lazy-src")
                    if (value := _text_attribute(image, name))
                ),
                "",
            )
            if not raw_url:
                continue
            attribution = _explicit_attribution(image)
            candidate = _candidate(
                raw_url=raw_url,
                source_page=canonical,
                allowed_hosts=allowed_hosts,
                origin="article_image",
                provenance_basis="source_page:article_img",
                alt=_text_attribute(image, "alt"),
                attribution=attribution,
                width=_dimension(image.get("width")),
                height=_dimension(image.get("height")),
                tag=image,
            )
            if candidate is not None:
                candidates.setdefault(candidate.url, candidate)

    meta_origins: dict[str, MediaOrigin] = {
        "og:image": "og_image",
        "og:image:url": "og_image",
        "twitter:image": "twitter_image",
        "twitter:image:src": "twitter_image",
    }
    for meta in soup.find_all("meta"):
        if not isinstance(meta, Tag):
            continue
        key = (_text_attribute(meta, "property") or _text_attribute(meta, "name")).casefold()
        origin = meta_origins.get(key)
        content = _text_attribute(meta, "content")
        if origin is None or not content:
            continue
        candidate = _candidate(
            raw_url=content,
            source_page=canonical,
            allowed_hosts=allowed_hosts,
            origin=origin,
            provenance_basis=f"source_page:meta:{key}",
            tag=meta,
        )
        if candidate is not None:
            candidates.setdefault(candidate.url, candidate)

    return list(candidates.values())


def download_article_media(
    candidate: ArticleMediaCandidate,
    destination: Path,
    client: httpx.Client,
    *,
    approved_image_hosts: frozenset[str] = frozenset(),
) -> DownloadedArticleMedia:
    """Download one reviewed candidate with bounded redirects and a content hash."""
    source_page, source_host = _source_page(candidate.source_page)
    allowed_hosts = _allowed_hosts(source_host, approved_image_hosts)
    url = _image_url(candidate.url, source_page, allowed_hosts)
    if client.follow_redirects:
        raise ArticleMediaError("automatic redirects must be disabled")

    data = bytearray()
    final_url = url
    for hop in range(_MAX_REDIRECTS):
        with client.stream("GET", final_url, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location or hop == _MAX_REDIRECTS - 1:
                    raise ArticleMediaError("invalid or excessive media redirect")
                final_url = _image_url(urljoin(final_url, location), source_page, allowed_hosts)
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_IMAGE_BYTES):
                raise ArticleMediaError("media exceeds size limit")
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise ArticleMediaError("media exceeds size limit")
                data.extend(chunk)
            break

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, extension = "image/png", ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        content_type, extension = "image/jpeg", ".jpg"
    else:
        raise ArticleMediaError("unsupported image signature")

    digest = hashlib.sha256(data).hexdigest()
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / (digest + extension)
    with path.open("xb") as handle:
        handle.write(data)
    return DownloadedArticleMedia(path, digest, content_type, len(data), final_url)
