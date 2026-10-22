"""Build a review-bound media candidate manifest from an already-saved source page."""

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relocant_media.collectors.article_media import extract_article_media_candidates

MAX_HTML_BYTES = 5 * 1024 * 1024


def _read_html_snapshot(path: Path) -> tuple[bytes, str]:
    if not path.is_file():
        raise ValueError("saved source HTML file is required")
    payload = path.read_bytes()
    if len(payload) > MAX_HTML_BYTES:
        raise ValueError("saved source HTML exceeds size limit")
    try:
        html = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("saved source HTML must be UTF-8") from exc
    return payload, html


def build_saved_page_manifest(
    source_page: str,
    html_file: Path,
    output: Path,
    *,
    image_hosts: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Extract candidates without network access, publishing, downloading, or rights approval."""
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")
    if output.exists():
        raise ValueError("new output directory required")

    payload, html = _read_html_snapshot(html_file)
    approved_hosts = frozenset(image_hosts)
    candidates = extract_article_media_candidates(
        html,
        source_page,
        approved_image_hosts=approved_hosts,
    )
    digest = hashlib.sha256(payload).hexdigest()
    result: dict[str, Any] = {
        "version": 1,
        "mode": "needs_visual_review" if candidates else "text",
        "source_page": source_page,
        "source_html_sha256": digest,
        "source_html_bytes": len(payload),
        "approved_image_hosts": sorted(approved_hosts),
        "observed_at": datetime.now(UTC).isoformat(),
        "candidates": [asdict(candidate) for candidate in candidates],
        "requires_independent_media_reviews": bool(candidates),
        "generated_media_used": False,
        "publication_permitted": False,
        "network_access_performed": False,
    }

    output.mkdir(parents=True, exist_ok=False)
    (output / "source.html").write_bytes(payload)
    (output / "manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract real-media candidates from a saved authoritative source page"
    )
    parser.add_argument("--source-page", required=True)
    parser.add_argument("--html-file", required=True, type=Path)
    parser.add_argument("--image-host", action="append", default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = build_saved_page_manifest(
            args.source_page,
            args.html_file,
            args.output,
            image_hosts=tuple(args.image_host),
        )
    except Exception:
        message = (
            "Извлечение media candidates остановлено: "
            "проверьте snapshot, source page и новый каталог.\n"
        )
        parser.exit(1, message)
    print(
        json.dumps(
            {
                "mode": result["mode"],
                "candidates": len(result["candidates"]),
                "manifest": str(args.output / "manifest.json"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
