import hashlib
import json
from pathlib import Path

import pytest

from relocant_media.ops.article_media_cli import MAX_HTML_BYTES, build_saved_page_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "article_media_generic.html"
SOURCE_PAGE = "https://news.official.example/articles/42"


def test_saved_source_page_builds_offline_review_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    output = tmp_path / "manifest"

    result = build_saved_page_manifest(
        SOURCE_PAGE,
        FIXTURE,
        output,
        image_hosts=("media.official.example",),
    )

    payload = FIXTURE.read_bytes()
    assert result["mode"] == "needs_visual_review"
    assert result["source_html_sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["source_html_bytes"] == len(payload)
    assert result["approved_image_hosts"] == ["media.official.example"]
    assert result["requires_independent_media_reviews"] is True
    assert result["generated_media_used"] is False
    assert result["publication_permitted"] is False
    assert result["network_access_performed"] is False
    assert len(result["candidates"]) == 4
    assert all(candidate["event_match"] == "unreviewed" for candidate in result["candidates"])
    assert all("rights_verified" not in candidate for candidate in result["candidates"])
    assert (output / "source.html").read_bytes() == payload
    stored = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert stored == result


def test_saved_manifest_without_cdn_allowlist_keeps_only_source_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    result = build_saved_page_manifest(SOURCE_PAGE, FIXTURE, tmp_path / "source-only")

    assert [candidate["url"] for candidate in result["candidates"]] == [
        "https://news.official.example/media/article-photo.jpg",
        "https://news.official.example/media/social-hero.jpg",
    ]


def test_staging_guard_runs_before_snapshot_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(ValueError, match="staging"):
        build_saved_page_manifest(SOURCE_PAGE, tmp_path / "missing.html", tmp_path / "out")


def test_saved_manifest_rejects_existing_output_and_large_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(ValueError, match="new output"):
        build_saved_page_manifest(SOURCE_PAGE, FIXTURE, output)

    large = tmp_path / "large.html"
    large.write_bytes(b"x" * (MAX_HTML_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        build_saved_page_manifest(SOURCE_PAGE, large, tmp_path / "large-out")
