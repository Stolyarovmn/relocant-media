"""Discover source covers and apply explicit assessments; never publish or call AI."""

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

import httpx

from relocant_media.collectors.beograd_article import match_beograd_article_url
from relocant_media.collectors.beograd_media import (
    download_beograd_media,
    parse_beograd_media,
)
from relocant_media.collectors.cukarica_media import (
    download_cukarica_media,
    parse_cukarica_media,
)
from relocant_media.collectors.cukarica_news import _article_url
from relocant_media.services.cover_selection import CoverAssessment, plan_cover

SourceKind = Literal["cukarica", "beograd"]


def _source_kind(article_url: str) -> SourceKind:
    if match_beograd_article_url(article_url) is not None:
        return "beograd"
    try:
        _article_url(article_url)
    except Exception:
        raise ValueError("unsupported article source for real-media discovery") from None
    return "cukarica"


def _identity(kind: SourceKind, url: str) -> tuple[str, str]:
    if kind == "cukarica":
        return _article_url(url)
    match = match_beograd_article_url(url)
    if match is None:
        raise ValueError("unsupported Beograd article URL")
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    canonical = urlunsplit(("https", "www.beograd.rs", parts.path, "", ""))
    return canonical, match.group("article_id")


def _discover(kind: SourceKind, html: str, source_page: str) -> list[Any]:
    if kind == "cukarica":
        return list(parse_cukarica_media(html, source_page))
    return list(parse_beograd_media(html, source_page))


def _download(
    kind: SourceKind,
    candidate: Any,
    destination: Path,
    client: httpx.Client,
) -> Any:
    if kind == "cukarica":
        return download_cukarica_media(candidate, destination, client)
    return download_beograd_media(candidate, destination, client)


def discover_and_plan(
    article_url: str, output: Path, *, assessments: Path | None = None,
    event_year: int | None = None, allow_album: bool = False,
) -> dict[str, Any]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")
    if output.exists():
        raise ValueError("new output directory required")

    kind = _source_kind(article_url)
    url, article_id = _identity(kind, article_url)
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        for _ in range(6):
            response = client.get(url)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("article redirect has no location")
                url, redirected_id = _identity(kind, urljoin(url, location))
                if redirected_id != article_id:
                    raise ValueError("article redirect changed identity")
                continue
            response.raise_for_status()
            break
        else:
            raise ValueError("too many redirects")

        candidates = _discover(kind, response.text, url)
        plan: dict[str, Any] = {
            "mode": "needs_visual_review" if candidates else "text",
            "selected": [],
            "requires_independent_media_reviews": bool(candidates),
        }
        if assessments is not None:
            if event_year is None:
                raise ValueError("event year required with assessments")
            values = json.loads(assessments.read_text(encoding="utf-8"))
            reviewed = tuple(CoverAssessment.model_validate(value) for value in values)
            known = {candidate.url for candidate in candidates}
            if any(item.image_url not in known or item.source_page != url for item in reviewed):
                raise ValueError("assessment is not bound to discovered article images")
            plan = plan_cover(reviewed, event_year=event_year, allow_album=allow_album)

        output.mkdir(parents=True, exist_ok=False)
        (output / "article.html").write_text(response.text, encoding="utf-8")
        downloads: list[dict[str, Any]] = []
        selected_urls = {item["image_url"] for item in plan["selected"]}
        for candidate in candidates:
            if candidate.url not in selected_urls:
                continue
            downloaded = _download(
                kind,
                candidate,
                output / f"asset-{len(downloads) + 1}",
                client,
            )
            downloads.append(
                {
                    **asdict(downloaded),
                    "path": str(downloaded.path.relative_to(output)),
                }
            )
        result = {
            "version": 2,
            "source_kind": kind,
            "source_page": url,
            "observed_at": datetime.now(UTC).isoformat(),
            "candidates": [asdict(item) for item in candidates],
            "plan": plan,
            "downloads": downloads,
            "generated_media_used": False,
            "publication_permitted": False,
        }
        (output / "manifest.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover and assess real official article covers")
    parser.add_argument("--article-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--assessments", type=Path)
    parser.add_argument("--event-year", type=int)
    parser.add_argument("--allow-album", action="store_true")
    args = parser.parse_args()
    try:
        result = discover_and_plan(**vars(args))
    except Exception:
        parser.exit(1, "Подбор обложки остановлен: проверьте источник и новый каталог выпуска.\n")
    print(
        json.dumps(
            {"mode": result["plan"]["mode"], "manifest": str(args.output / "manifest.json")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
