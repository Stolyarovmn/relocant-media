from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.beograd_article import ArticleExtractionError
from relocant_media.collectors.beograd_media import (
    BeogradMediaCandidate,
    download_beograd_media,
    parse_beograd_media,
)

FIXTURE = Path(__file__).parent / "fixtures" / "beograd_article_media.html"
ARTICLE_URL = (
    "https://www.beograd.rs/lat/beoinfo-vesti/a116251/"
    "Sapic-obisao-radove-na-kapitalnim-projektima.html"
)
IMAGE_URL = (
    "https://static.beograd.rs/api/v3/images/90350/medium"
    "?ts=2026-10-16T13%3A54%3A23"
)


def test_discovery_extracts_real_article_image_and_provenance_only() -> None:
    items = parse_beograd_media(FIXTURE.read_text(encoding="utf-8"), ARTICLE_URL)

    assert items == [
        BeogradMediaCandidate(
            url=IMAGE_URL,
            alt="2.jpeg",
            credit="Foto: Grad Beograd",
            source_page=ARTICLE_URL,
            rights_hint="explicit_credit_requires_review",
        )
    ]
    assert all("example.test" not in item.url for item in items)


def test_discovery_without_credit_never_claims_reuse_rights() -> None:
    html = FIXTURE.read_text(encoding="utf-8").replace(
        "<figcaption>Foto: Grad Beograd</figcaption>", ""
    )
    items = parse_beograd_media(html, ARTICLE_URL)
    assert len(items) == 1
    assert items[0].credit == ""
    assert items[0].rights_hint == "rights_unconfirmed"


def test_discovery_rejects_unsupported_article_and_ignores_untrusted_images() -> None:
    with pytest.raises(ArticleExtractionError, match="unsupported Beograd article URL"):
        parse_beograd_media(FIXTURE.read_text(encoding="utf-8"), "https://example.test/news")

    html = FIXTURE.read_text(encoding="utf-8").replace(
        IMAGE_URL,
        "https://evil.example/api/v3/images/90350/medium",
    )
    assert parse_beograd_media(html, ARTICLE_URL) == []


def test_downloader_is_bounded_and_verifies_real_image_signature(tmp_path: Path) -> None:
    candidate = parse_beograd_media(FIXTURE.read_text(encoding="utf-8"), ARTICLE_URL)[0]
    jpeg = b"\xff\xd8\xff" + b"real-photo-bytes"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=jpeg,
            headers={"content-length": str(len(jpeg)), "content-type": "image/jpeg"},
            request=request,
        )

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
    ) as client:
        downloaded = download_beograd_media(candidate, tmp_path / "asset", client)

    assert requests[0].method == "GET"
    assert str(requests[0].url) == IMAGE_URL
    assert downloaded.path.read_bytes() == jpeg
    assert downloaded.content_type == "image/jpeg"
    assert downloaded.size_bytes == len(jpeg)
    assert downloaded.source_url == IMAGE_URL
    assert len(downloaded.sha256) == 64


def test_downloader_fails_closed_on_redirect_host_and_bad_signature(tmp_path: Path) -> None:
    candidate = parse_beograd_media(FIXTURE.read_text(encoding="utf-8"), ARTICLE_URL)[0]

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302, headers={"location": "https://evil.example/photo.jpg"}, request=request
            )
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        with pytest.raises(ArticleExtractionError, match="unsupported Beograd media URL"):
            download_beograd_media(candidate, tmp_path / "redirect", client)

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not-an-image", request=request)
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        with pytest.raises(ArticleExtractionError, match="unsupported image signature"):
            download_beograd_media(candidate, tmp_path / "bad", client)
