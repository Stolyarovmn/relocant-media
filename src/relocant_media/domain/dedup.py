"""Deterministic URL/content/event deduplication helpers.

The rules in this module are intentionally model-free. They are conservative: exact
URL/content matches are always duplicates, while fuzzy event matching is allowed only
across different sources and inside a bounded publication window.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "yclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}
_WORDS = re.compile(r"[^\w]+", flags=re.UNICODE)
DEFAULT_EVENT_WINDOW = timedelta(hours=48)
DEFAULT_TITLE_SIMILARITY = 0.78


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(part for part in _WORDS.sub(" ", normalized).split() if part)


def canonicalize_url(value: str) -> str:
    """Canonicalize an HTTP(S) URL without changing its semantic path."""
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return raw

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    port = parsed.port
    if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = hostname
    else:
        netloc = f"{hostname}:{port}"

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"

    query = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in _TRACKING_KEYS:
            continue
        query.append((key, item_value))
    query.sort(key=lambda item: (item[0].casefold(), item[1]))
    return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))


def content_sha256(title: str, body: str) -> str:
    payload = f"{normalize_text(title)}\n{normalize_text(body)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def title_similarity(left: str, right: str) -> float:
    left_tokens = set(normalize_text(left).split())
    right_tokens = set(normalize_text(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union)


@dataclass(frozen=True, slots=True)
class EventCandidate:
    raw_item_id: int
    source_id: str
    url: str
    title: str
    body: str
    published_at: datetime | None
    canonical_url: str | None = None
    content_sha256: str | None = None

    @property
    def resolved_canonical_url(self) -> str:
        return self.canonical_url or canonicalize_url(self.url)

    @property
    def resolved_content_sha256(self) -> str:
        return self.content_sha256 or content_sha256(self.title, self.body)


def same_event(
    left: EventCandidate,
    right: EventCandidate,
    *,
    event_window: timedelta = DEFAULT_EVENT_WINDOW,
    title_threshold: float = DEFAULT_TITLE_SIMILARITY,
) -> bool:
    if left.resolved_canonical_url == right.resolved_canonical_url:
        return True
    if left.resolved_content_sha256 == right.resolved_content_sha256:
        return True

    # Fuzzy matching is deliberately limited to independent sources. Within one
    # publisher we require an exact URL/content match to avoid merging recurring
    # columns or boilerplate headlines into a single event.
    if left.source_id == right.source_id:
        return False
    if left.published_at is None or right.published_at is None:
        return False
    if abs(left.published_at - right.published_at) > event_window:
        return False
    return title_similarity(left.title, right.title) >= title_threshold


def cluster_events(candidates: list[EventCandidate]) -> list[list[EventCandidate]]:
    """Cluster candidates deterministically, preserving the earliest representative."""
    ordered = sorted(
        candidates,
        key=lambda item: (
            item.published_at or datetime.max.replace(tzinfo=UTC),
            item.raw_item_id,
        ),
    )
    clusters: list[list[EventCandidate]] = []
    for candidate in ordered:
        for cluster in clusters:
            if any(same_event(candidate, member) for member in cluster):
                cluster.append(candidate)
                break
        else:
            clusters.append([candidate])
    return clusters


def event_key(candidate: EventCandidate) -> str:
    seed = "\n".join(
        (
            candidate.resolved_canonical_url,
            candidate.resolved_content_sha256,
            normalize_text(candidate.title),
        )
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()
