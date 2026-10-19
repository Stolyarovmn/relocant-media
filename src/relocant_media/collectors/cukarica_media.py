"""Discover official article images; discovery never grants reuse permission."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup, Comment, Tag

from relocant_media.collectors.cukarica_news import CukaricaExtractionError, _article_url

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_PATH = re.compile(r"/admin/uploads/images/[A-Za-z0-9_-]+\.(?:jpe?g|png)", re.I)


@dataclass(frozen=True)
class CukaricaMediaCandidate:
    url: str
    alt: str
    source_page: str


@dataclass(frozen=True)
class DownloadedCukaricaMedia:
    path: Path
    sha256: str
    content_type: str
    size_bytes: int
    source_url: str


def _image_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in {"www.cukarica.rs", "cukarica.rs"}
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.query
            or parts.fragment
            or IMAGE_PATH.fullmatch(parts.path) is None
        ):
            raise ValueError
    except ValueError as exc:
        raise CukaricaExtractionError("unsupported Cukarica media URL") from exc
    return urlunsplit(("https", "www.cukarica.rs", parts.path, "", ""))


def parse_cukarica_media(html: str, article_url: str) -> list[CukaricaMediaCandidate]:
    """Read only the featured-image section, never related stories or page logos."""
    canonical, _ = _article_url(article_url)
    soup = BeautifulSoup(html, "html.parser")
    roots = soup.select(".page-full-width")
    if len(roots) != 1:
        raise CukaricaExtractionError("article root missing or ambiguous")
    root = roots[0]
    markers = [
        node
        for node in root.find_all(string=lambda x: isinstance(x, Comment))
        if str(node).strip() == "Featured Image"
    ]
    if len(markers) != 1 or markers[0].parent is not root:
        raise CukaricaExtractionError("featured image marker missing or ambiguous")
    section = markers[0].find_next_sibling()
    if not isinstance(section, Tag) or section.parent is not root:
        raise CukaricaExtractionError("featured image section missing")
    next_markers = [node for node in section.next_siblings if isinstance(node, Comment)]
    if not next_markers or str(next_markers[0]).strip() != "Excerpt":
        raise CukaricaExtractionError("featured image boundary changed")
    candidates: dict[str, CukaricaMediaCandidate] = {}
    for image in section.find_all("img"):
        src = image.get("src")
        alt = image.get("alt", "")
        if not isinstance(src, str) or not isinstance(alt, str):
            raise CukaricaExtractionError("invalid featured image")
        url = _image_url(urljoin(canonical, src))
        candidates[url] = CukaricaMediaCandidate(url, alt.strip(), canonical)
    return list(candidates.values())


def download_cukarica_media(
    candidate: CukaricaMediaCandidate,
    destination: Path,
    client: httpx.Client,
) -> DownloadedCukaricaMedia:
    """Explicit bounded download into a new immutable directory, without rights inference."""
    _article_url(candidate.source_page)
    url = _image_url(candidate.url)
    if client.follow_redirects:
        raise CukaricaExtractionError("automatic redirects must be disabled")
    data = bytearray()
    for hop in range(4):
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.is_redirect:
                location = response.headers.get("location")
                if not location or hop == 3:
                    raise CukaricaExtractionError("invalid or excessive media redirect")
                url = _image_url(urljoin(url, location))
                continue
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_IMAGE_BYTES):
                raise CukaricaExtractionError("media exceeds size limit")
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise CukaricaExtractionError("media exceeds size limit")
                data.extend(chunk)
            break
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, extension = "image/png", ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        content_type, extension = "image/jpeg", ".jpg"
    else:
        raise CukaricaExtractionError("unsupported image signature")
    digest = hashlib.sha256(data).hexdigest()
    destination.mkdir(parents=True, exist_ok=False)
    path = destination / (digest + extension)
    with path.open("xb") as handle:
        handle.write(data)
    return DownloadedCukaricaMedia(path, digest, content_type, len(data), url)
