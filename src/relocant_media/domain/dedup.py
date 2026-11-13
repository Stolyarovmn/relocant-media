"""Deterministic URL/content/event deduplication helpers.

The rules in this module are intentionally model-free. They are conservative: exact
URL/content matches are always duplicates, cross-source fuzzy matching stays bounded,
and same-source fuzzy matching is allowed only for strongly anchored transport reprints.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from relocant_media.domain.event_facts import (
    clock_ranges,
    conflicting_facts,
    enhanced_service_anchor,
    event_dates,
    location_anchors,
    material_words,
    numeric_facts,
    street_anchors,
    terminal_anchor,
    transport_routes,
)
from relocant_media.domain.topic_tags import is_topic_footer

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
        following_year = re.match(r"\s+(\d{4})\b", normalized[match.end() :])
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
class _EventFacts:
    numbers: frozenset[str]
    material: frozenset[str]
    locations: frozenset[str]

    def conflicts_with(self, other: _EventFacts) -> bool:
        return (
            self.numbers != other.numbers
            or self.material != other.material
            or bool(self.locations and other.locations and self.locations != other.locations)
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
    # Кэш принадлежит неизменяемому снимку, а не глобальной истории процесса.
    # dataclasses.replace создаёт чистый кэш при исправлении текста или даты.
    _canonical_cache: str | None = field(default=None, init=False, repr=False, compare=False)
    _content_cache: str | None = field(default=None, init=False, repr=False, compare=False)
    _title_tokens_cache: frozenset[str] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _body_facts_cache: _EventFacts | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _document_facts_cache: _EventFacts | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def resolved_canonical_url(self) -> str:
        if self._canonical_cache is None:
            object.__setattr__(
                self, "_canonical_cache", self.canonical_url or canonicalize_url(self.url)
            )
        assert self._canonical_cache is not None
        return self._canonical_cache

    @property
    def resolved_content_sha256(self) -> str:
        if self._content_cache is None:
            object.__setattr__(
                self,
                "_content_cache",
                self.content_sha256 or content_sha256(self.title, self.body),
            )
        assert self._content_cache is not None
        return self._content_cache

    def _facts(self, *, include_title: bool) -> _EventFacts:
        cache_name = "_document_facts_cache" if include_title else "_body_facts_cache"
        cached = self._document_facts_cache if include_title else self._body_facts_cache
        if cached is None:
            text = f"{self.title}\n{self.body}" if include_title else self.body
            cached = _EventFacts(
                numeric_facts(text, self.published_at.year if self.published_at else 0),
                material_words(text),
                location_anchors(text),
            )
            object.__setattr__(self, cache_name, cached)
        return cached

    def _title_similarity(self, other: EventCandidate) -> float:
        for candidate in (self, other):
            if candidate._title_tokens_cache is None:
                object.__setattr__(
                    candidate,
                    "_title_tokens_cache",
                    frozenset(normalize_text(candidate.title).split()),
                )
        left, right = self._title_tokens_cache, other._title_tokens_cache
        assert left is not None and right is not None
        return len(left & right) / len(left | right) if left and right else 0.0


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

    if (
        left._facts(include_title=False).numbers != right._facts(include_title=False).numbers
        or left._facts(include_title=True).material != right._facts(include_title=True).material
    ):
        return False

    if _anchored_transport_reprint(left, right):
        return True

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


def _anchored_transport_reprint(left: EventCandidate, right: EventCandidate) -> bool:
    """Две ограниченные формы объявления, не общий смысловой классификатор."""
    if max(len(left.body), len(right.body)) > _SAME_SOURCE_MAX_BODY_CHARS:
        return False
    assert left.published_at is not None and right.published_at is not None
    dates = event_dates(left.body, left.published_at.year)
    if not dates or dates != event_dates(right.body, right.published_at.year):
        return False
    routes = transport_routes(left.body)
    if len(routes) < 2 or routes != transport_routes(right.body):
        return False
    left_facts = left._facts(include_title=False)
    right_facts = right._facts(include_title=False)
    if left_facts.numbers != right_facts.numbers or left_facts.material != right_facts.material:
        return False

    # Сокращённый маршрут: совпадают улицы, конечная, все маршруты и период.
    terminal = terminal_anchor(left.body)
    streets = street_anchors(left.body)
    if (
        terminal
        and terminal == terminal_anchor(right.body)
        and len(streets) >= 2
        and streets == street_anchors(right.body)
    ):
        return True

    # Усиление движения: кроме большой группы маршрутов нужны одинаковые
    # часы и временный маршрут с обоими концами (в том же порядке).
    enhanced = enhanced_service_anchor(left.body)
    return bool(
        len(routes) >= 5
        and enhanced
        and enhanced == enhanced_service_anchor(right.body)
        and clock_ranges(left.body)
        and clock_ranges(left.body) == clock_ranges(right.body)
    )


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

    # Точные совпадения не ограничены датой; все неточные требуют этого окна.
    # Отсекаем старую историю до разбора URL, заголовков и полных текстов.
    if left.published_at is None or right.published_at is None:
        return False
    if abs(left.published_at - right.published_at) > event_window:
        return False

    # Материализованная правка первоисточника уже назначена на новое ревью.
    # Не превращаем её обратно в прочитанный исходный материал через fuzzy.
    if any(
        "__source_revision" in dict(parse_qsl(urlsplit(item.resolved_canonical_url).query))
        for item in (left, right)
    ):
        return False

    if left.source_id == right.source_id:
        return _same_source_transport_reprint(left, right, event_window=event_window)
    # Разные заголовки уже исключают межисточниковый fuzzy-повтор.
    # Разбор фактов нужен только оставшимся похожим парам: inbox сравнивает
    # тысячи материалов, и иначе повторно сканирует каждый body для каждой пары.
    if not left._title_similarity(right) >= title_threshold:
        return False
    if left._facts(include_title=True).conflicts_with(right._facts(include_title=True)):
        return False
    return True


def find_repeated_publication(
    title: str,
    body: str,
    publications: Sequence[tuple[int, str, str]],
) -> int | None:
    """Ищет дословный/почти дословный повтор уже опубликованного содержания.

    Вызывающий код задаёт город и временное окно публикаций. Это отдельный
    контроль готового текста: исходные статьи, хеши и квитанции не меняются.
    Короткие тексты сравниваем только точно; более свободные пересказы требуют
    редакционной оценки новизны, а не удаления по одному счёту или названию.
    """
    body = _publication_content(body)
    title = re.sub(r"^\s*\[тестовый запуск\]\s*", "", title, flags=re.I)
    normalized_body = normalize_text(body)
    if not normalized_body:
        return None
    for publication_id, old_title, old_body in publications:
        old_body = _publication_content(old_body)
        old_title = re.sub(r"^\s*\[тестовый запуск\]\s*", "", old_title, flags=re.I)
        previous = normalize_text(old_body)
        if conflicting_facts(f"{title}\n{body}", f"{old_title}\n{old_body}", 0, 0):
            continue
        if normalized_body == previous:
            return publication_id
        if min(len(normalized_body), len(previous)) < 160:
            continue
        if max(len(normalized_body), len(previous)) > _SAME_SOURCE_MAX_BODY_CHARS:
            continue
        if title_similarity(title, old_title) < 0.9:
            continue
        if _body_token_similarity(body, old_body) < 0.96:
            continue
        if SequenceMatcher(None, normalized_body, previous, autojunk=False).ratio() >= 0.97:
            return publication_id
    return None


def _publication_content(value: str) -> str:
    """Убирает только известные отдельные строки оформления публикации."""
    plain = re.sub(
        r"(?m)^(?:Рубрика: [^\r\n]+|Карта из источника: https?://\S+)\r?$",
        "",
        value,
    ).strip()
    content, separator, footer = plain.rpartition("\n")
    return content.rstrip() if separator and is_topic_footer(footer) else plain


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
    by_url: dict[str, set[int]] = defaultdict(set)
    by_content: dict[str, set[int]] = defaultdict(set)
    active: deque[tuple[datetime, int]] = deque()
    active_counts: dict[int, int] = {}
    for candidate in ordered:
        url, fingerprint = candidate.resolved_canonical_url, candidate.resolved_content_sha256
        possible = by_url[url] | by_content[fingerprint]
        if candidate.published_at is not None:
            lower = candidate.published_at - DEFAULT_EVENT_WINDOW
            while active and active[0][0] < lower:
                _, expired = active.popleft()
                active_counts[expired] -= 1
                if active_counts[expired] == 0:
                    del active_counts[expired]
            possible.update(active_counts)
        # Сохраняем прежнее правило: присоединение к самой ранней подходящей
        # группе, без слияния остальных групп даже при транзитивном совпадении.
        for cluster_index in sorted(possible):
            cluster = clusters[cluster_index]
            if any(same_event(candidate, member) for member in cluster):
                cluster.append(candidate)
                break
        else:
            cluster_index = len(clusters)
            clusters.append([candidate])
        by_url[url].add(cluster_index)
        by_content[fingerprint].add(cluster_index)
        if candidate.published_at is not None:
            active.append((candidate.published_at, cluster_index))
            active_counts[cluster_index] = active_counts.get(cluster_index, 0) + 1
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
