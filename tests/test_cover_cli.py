import json
from pathlib import Path

import httpx
import pytest
from test_cover_selection import assessment
from test_cukarica_media import ARTICLE, HTML, IMAGE, PNG

from relocant_media.ops import cover_cli

BEOGRAD_ARTICLE = (
    "https://www.beograd.rs/lat/beoinfo-vesti/a116251/"
    "Sapic-obisao-radove-na-kapitalnim-projektima.html"
)
BEOGRAD_IMAGE = (
    "https://static.beograd.rs/api/v3/images/90350/medium"
    "?ts=2026-10-16T13%3A54%3A23"
)
BEOGRAD_HTML = (
    Path(__file__).parent / "fixtures" / "beograd_article_media.html"
).read_text(encoding="utf-8")
JPEG = b"\xff\xd8\xffreal-beograd-photo"


def test_discovery_requires_review_and_only_explicit_assessment_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    calls = []
    original = httpx.Client

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=HTML) if str(request.url) == ARTICLE else (
            httpx.Response(200, content=PNG)
        )

    monkeypatch.setattr(cover_cli.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(handler), **kw,
    ))
    discovery = cover_cli.discover_and_plan(ARTICLE, tmp_path / "discovery")
    assert discovery["plan"]["mode"] == "needs_visual_review" and calls == [ARTICLE]
    assert discovery["generated_media_used"] is False
    reviewed = assessment(image_url=IMAGE, source_page=ARTICLE)
    file = tmp_path / "assessments.json"
    file.write_text(json.dumps([reviewed.model_dump(mode="json")]), encoding="utf-8")
    result = cover_cli.discover_and_plan(
        ARTICLE, tmp_path / "selected", assessments=file, event_year=2026,
    )
    assert result["plan"]["mode"] == "single_cover"
    assert len(result["downloads"]) == 1 and calls.count(IMAGE) == 1
    assert result["publication_permitted"] is False
    assert (tmp_path / "selected" / result["downloads"][0]["path"]).read_bytes() == PNG
    with pytest.raises(ValueError, match="new output"):
        cover_cli.discover_and_plan(ARTICLE, tmp_path / "selected")


def test_beograd_real_image_uses_same_review_bound_cover_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    calls: list[str] = []
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == BEOGRAD_ARTICLE:
            return httpx.Response(200, text=BEOGRAD_HTML, request=request)
        if str(request.url) == BEOGRAD_IMAGE:
            return httpx.Response(200, content=JPEG, request=request)
        return httpx.Response(404, request=request)

    monkeypatch.setattr(
        cover_cli.httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )
    discovery = cover_cli.discover_and_plan(BEOGRAD_ARTICLE, tmp_path / "discovery")
    assert discovery["source_kind"] == "beograd"
    assert discovery["plan"]["mode"] == "needs_visual_review"
    assert discovery["generated_media_used"] is False
    assert discovery["candidates"] == [
        {
            "url": BEOGRAD_IMAGE,
            "alt": "2.jpeg",
            "credit": "Foto: Grad Beograd",
            "source_page": BEOGRAD_ARTICLE,
            "rights_hint": "explicit_credit_requires_review",
        }
    ]
    assert calls == [BEOGRAD_ARTICLE]

    reviewed = assessment(image_url=BEOGRAD_IMAGE, source_page=BEOGRAD_ARTICLE)
    file = tmp_path / "beograd-assessments.json"
    file.write_text(json.dumps([reviewed.model_dump(mode="json")]), encoding="utf-8")
    selected = cover_cli.discover_and_plan(
        BEOGRAD_ARTICLE,
        tmp_path / "selected",
        assessments=file,
        event_year=2026,
    )
    assert selected["plan"]["mode"] == "single_cover"
    assert selected["publication_permitted"] is False
    assert selected["generated_media_used"] is False
    assert len(selected["downloads"]) == 1
    assert (tmp_path / "selected" / selected["downloads"][0]["path"]).read_bytes() == JPEG


def test_staging_required_before_any_io(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(ValueError, match="staging"):
        cover_cli.discover_and_plan(ARTICLE, tmp_path / "out")


def test_foreign_assessment_cannot_select_an_unobserved_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    original = httpx.Client
    monkeypatch.setattr(cover_cli.httpx, "Client", lambda **kw: original(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=HTML)), **kw,
    ))
    file = tmp_path / "assessments.json"
    file.write_text(json.dumps([assessment().model_dump(mode="json")]), encoding="utf-8")
    with pytest.raises(ValueError, match="not bound"):
        cover_cli.discover_and_plan(ARTICLE, tmp_path / "out", assessments=file, event_year=2026)
    assert not (tmp_path / "out").exists()
