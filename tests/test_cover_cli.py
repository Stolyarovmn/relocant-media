import json
from pathlib import Path

import httpx
import pytest
from test_cover_selection import assessment
from test_cukarica_media import ARTICLE, HTML, IMAGE, PNG

from relocant_media.ops import cover_cli


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
