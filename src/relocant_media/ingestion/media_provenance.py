"""Build media candidate provenance from source HTML already fetched by a collector."""

from dataclasses import asdict
from hashlib import sha256
from typing import Any

from relocant_media.collectors.article_media import (
    ArticleMediaError,
    extract_article_media_candidates,
)
from relocant_media.collectors.base import RawItem

MAX_SOURCE_HTML_BYTES = 5 * 1024 * 1024


def build_media_provenance(item: RawItem) -> tuple[str | None, dict[str, Any] | None]:
    """Return saved HTML and a fail-closed manifest without performing network I/O."""
    if item.source_html is None:
        return None, None

    encoded = item.source_html.encode("utf-8")
    digest = sha256(encoded).hexdigest()
    approved_hosts = sorted(set(item.approved_image_hosts))
    base: dict[str, Any] = {
        "version": 1,
        "source_page": item.url,
        "source_html_sha256": digest,
        "source_html_bytes": len(encoded),
        "approved_image_hosts": approved_hosts,
        "source_fetch_reused": True,
        "candidate_extraction_network_access_performed": False,
        "generated_media_used": False,
        "publication_permitted": False,
    }

    if len(encoded) > MAX_SOURCE_HTML_BYTES:
        return None, {
            **base,
            "source_html_saved": False,
            "candidate_extraction_status": "omitted_oversize",
            "candidates": [],
            "requires_independent_media_reviews": False,
            "mode": "text",
        }

    try:
        candidates = extract_article_media_candidates(
            item.source_html,
            item.url,
            approved_image_hosts=frozenset(item.approved_image_hosts),
        )
    except ArticleMediaError:
        return item.source_html, {
            **base,
            "source_html_saved": True,
            "candidate_extraction_status": "rejected",
            "candidates": [],
            "requires_independent_media_reviews": False,
            "mode": "text",
        }

    serialized = [asdict(candidate) for candidate in candidates]
    return item.source_html, {
        **base,
        "source_html_saved": True,
        "candidate_extraction_status": "extracted",
        "candidates": serialized,
        "requires_independent_media_reviews": bool(serialized),
        "mode": "needs_visual_review" if serialized else "text",
    }
