"""Deterministic URL/content/event deduplication helpers.

The rules in this module are intentionally model-free. They are conservative: exact
URL/content matches are always duplicates, cross-source fuzzy matching stays bounded,
and same-source fuzzy matching is allowed only for strongly anchored transport reprints.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
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
_EVENT_DATE_ANCHOR = re.compile(
    r"\b\d{1,2}\.\s*(?:\d{1,2}(?:\.\s*\d{2,4})?|[^\W\d_]{3,14})",
    flags=re.UNICODE,
)
_STOP_ID = re.compile(r"#\s*(\d{3,5})")
_ROUTE_INTRO = re.compile(
    r"\blinij\w*(?:\s+(?:broj|br))?\s+",
    flags=re.IGNORECASE | re.UNICODE,
)
_ROUTE_ID = re.compile(r"^\d{1,3}[a-z]?$", flags=re.IGNORECASE)
_TRANSPORT_ACTION = re.compile(
    r"\b(?:ukid\w*|izmešt\w*|izmest\w*|uspostav\w*|zatvar\w*|"
    r"u?dvosmer\w*|jednosmer\w*)\b",
    flags=re.IGNORECASE | re.UNICODE,
)
DEFAULT_EVENT_WINDOW = timedelta(hours=48)
DEFAULT_TITLE_SIMILARITY = 0.78
_SAME_SOURCE_BODY_SEQUENCE_SIMILARITY = 0.85
_SAME_SOURCE_BODY_TOKEN_SIMILARITY = 0.80
_SAME_SOURCE_MAX_BODY_CHARS = 8_000
_TRANSPORT_NEGATION = re.compile(r"\b(?:ne|neće|nece|nije|nisu|nema|bez)\b")


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
    payload = f"{normalize_text(title)}\n{normalize_text(body)}".encode()
    return hashlib.sha256(payload).hexdigest()


def title_similarity(left: str, right: str) -> float:
    left_tokens = set(normalize_text(left).split())
    right_tokens = set(normalize_text(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union)


def _body_token_similarity(left: str, right: str) -> float:
    left_tokens = set(normalize_text(left).split())
    right_tokens = set(normalize_text(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _event_date_anchors(value: str, *, publication_year: int | None = None) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    dates = set()
    for match in _EVENT_DATE_ANCHOR.finditer(normalized):
        anchor = normalize_text(match.group(0))
        # The month-name branch ends before the optional year. Never erase an
        # explicitly different event year when comparing annual announcements.
        following_year = re.match(r"\s+(\d{4})\b", normalized[match.end():])
        if following_year:
            anchor += " " + following_year.group(1)
        elif publication_year is not None and not re.search(r"\b\d{4}\b", anchor):
            anchor += " " + str(publication_year)
        dates.add(anchor)
    return frozenset(dates)


def _transport_routes(value: str) -> frozenset[str]:
    normalized = normalize_text(value)
    routes: set[str] = set()
    for intro in _ROUTE_INTRO.finditer(normalized):
        for token in normalized[intro.end() :].split():
            if token == "i":
                continue
            if _ROUTE_ID.fullmatch(token) is None:
                break
            routes.add(token.upper())
    return frozenset(routes)


def _transport_stop_ids(value: str) -> frozenset[str]:
    return frozenset(_STOP_ID.findall(value))


def _transport_actions(value: str) -> frozenset[str]:
    normalized = normalize_text(value)
    return frozenset(
        "dvosmer" if match.group(0).startswith(("dvosmer", "udvosmer")) else match.group(0)
        for match in _TRANSPORT_ACTION.finditer(normalized)
    )


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


def _same_source_transport_reprint(
    left: EventCandidate,
    right: EventCandidate,
    *,
    event_window: timedelta,
) -> bool:
    if left.source_id != right.source_id:
        return False
    if left.published_at is None or right.published_at is None:
        return False
    if abs(left.published_at - right.published_at) > event_window:
        return False

    left_host = urlsplit(left.resolved_canonical_url).hostname
    right_host = urlsplit(right.resolved_canonical_url).hostname
    if left_host is None or left_host != right_host:
        return False

    left_dates = _event_date_anchors(left.body, publication_year=left.published_at.year)
    right_dates = _event_date_anchors(right.body, publication_year=right.published_at.year)
    if not left_dates or left_dates != right_dates:
        return False

    left_routes = _transport_routes(left.body)
    right_routes = _transport_routes(right.body)
    if len(left_routes) < 2 or left_routes != right_routes:
        return False

    left_stops = _transport_stop_ids(left.body)
    right_stops = _transport_stop_ids(right.body)
    if not left_stops or left_stops != right_stops:
        return False

    left_actions = _transport_actions(left.body)
    right_actions = _transport_actions(right.body)
    if not left_actions or left_actions != right_actions:
        return False

    left_body = normalize_text(left.body)
    right_body = normalize_text(right.body)
    if sorted(_TRANSPORT_NEGATION.findall(left_body)) != sorted(
        _TRANSPORT_NEGATION.findall(right_body)
    ):
        return False
    # Disabling character-popularity suppression keeps ordinary Serbian letters
    # usable as evidence. Bound this potentially quadratic comparison first.
    if max(len(left_body), len(right_body)) > _SAME_SOURCE_MAX_BODY_CHARS:
        return False
    if (
        SequenceMatcher(None, left_body, right_body, autojunk=False).ratio()
        < _SAME_SOURCE_BODY_SEQUENCE_SIMILARITY
    ):
        return False
    return _body_token_similarity(left.body, right.body) >= _SAME_SOURCE_BODY_TOKEN_SIMILARITY


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

    if left.source_id == right.source_id:
        return _same_source_transport_reprint(left, right, event_window=event_window)
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
    return hashlib.sha256(seed.encode()).hexdigest()
