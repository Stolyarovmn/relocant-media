import hashlib
from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.article_media import (
    ArticleMediaError,
    download_article_media,
    extract_article_media_candidates,
)

FIXTURE = Path(__file__).parent / "fixtures" / "article_media_generic.html"
SOURCE_PAGE = "https://news.official.example/articles/42"
APPROVED_CDN = frozenset({"media.official.example"})


def test_extracts_source_related_candidates_without_approving_them() -> None:
    candidates = extract_article_media_candidates(
        FIXTURE.read_text(encoding="utf-8"),
        SOURCE_PAGE,
        approved_image_hosts=APPROVED_CDN,
    )

    assert [candidate.url for candidate in candidates] == [
        "https://news.official.example/media/article-photo.jpg",
        "https://media.official.example/event-poster.jpg",
        "https://news.official.example/media/social-hero.jpg",
        "https://media.official.example/twitter-card.jpg",
    ]
    assert candidates[0].origin == "article_image"
    assert candidates[0].attribution == "Foto: Grad Beograd"
    assert candidates[0].rights_hint == "explicit_attribution_requires_rights_review"
    assert candidates[0].provenance_basis == "source_page:article_img"
    assert all(candidate.source_page == SOURCE_PAGE for candidate in candidates)
    assert all(candidate.event_match == "unreviewed" for candidate in candidates)
    assert all("evil.example" not in candidate.url for candidate in candidates)
    assert all("logo" not in candidate.url for candidate in candidates)
    assert all("tracker" not in candidate.url for candidate in candidates)


def test_external_image_host_requires_explicit_allowlist() -> None:
    candidates = extract_article_media_candidates(
        FIXTURE.read_text(encoding="utf-8"),
        SOURCE_PAGE,
    )

    assert [candidate.url for candidate in candidates] == [
        "https://news.official.example/media/article-photo.jpg",
        "https://news.official.example/media/social-hero.jpg",
    ]


def test_downloader_records_hash_and_revalidates_redirects(tmp_path: Path) -> None:
    candidate = extract_article_media_candidates(
        FIXTURE.read_text(encoding="utf-8"),
        SOURCE_PAGE,
        approved_image_hosts=APPROVED_CDN,
    )[0]
    jpeg = b"\xff\xd8\xff" + b"official-photo-bytes"

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=jpeg,
                headers={"content-length": str(len(jpeg))},
                request=request,
            )
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        downloaded = download_article_media(candidate, tmp_path / "asset", client)

    assert downloaded.path.read_bytes() == jpeg
    assert downloaded.sha256 == hashlib.sha256(jpeg).hexdigest()
    assert downloaded.content_type == "image/jpeg"
    assert downloaded.size_bytes == len(jpeg)
    assert downloaded.source_url == candidate.url

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://evil.example/stolen.jpg"},
                request=request,
            )
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        with pytest.raises(ArticleMediaError, match="unsupported image URL"):
            download_article_media(candidate, tmp_path / "redirect", client)


def test_downloader_rejects_non_image_bytes(tmp_path: Path) -> None:
    candidate = extract_article_media_candidates(
        FIXTURE.read_text(encoding="utf-8"),
        SOURCE_PAGE,
    )[0]

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not-an-image", request=request)
        ),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        with pytest.raises(ArticleMediaError, match="unsupported image signature"):
            download_article_media(candidate, tmp_path / "bad", client)
