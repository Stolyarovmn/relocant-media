"""Bounded primary-source discovery; topic similarity never grants factual approval."""

import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from relocant_media.collectors.aero_telegraf import SOURCE_ID as AVIATION_SOURCE
from relocant_media.collectors.aero_telegraf import article_id as aviation_article_id
from relocant_media.collectors.base import RawItem
from relocant_media.collectors.beograd_article import match_beograd_article_url
from relocant_media.collectors.bgsaobracaj_notices import SOURCE_ID as TRAFFIC_SECRETARIAT_SOURCE
from relocant_media.collectors.bgsaobracaj_notices import match_bgsaobracaj_notice
from relocant_media.collectors.cukarica_news import ARTICLE_PATH
from relocant_media.collectors.venue_feed import _allowed
from relocant_media.domain.flight_notices import city_carriers
from relocant_media.sources.registry import SourceConfig

_LETTERS = dict(zip(
    "абвгдђежзијклљмнњопрстћуфхцчџшёыэюяьъ",
    ("a", "b", "v", "g", "d", "dj", "e", "z", "z", "i", "j", "k", "l", "lj", "m", "n",
     "nj", "o", "p", "r", "s", "t", "c", "u", "f", "h", "c", "c", "dz", "s", "e", "y",
     "e", "ju", "ja", "", ""), strict=True))
_STOP = set("koncert concert koncerti koncertna concerti festival events event predstoji "
            "beograd beograda belgrad belgrade novosti news novost segodnja zavtra "
            "sostoitsja projdet okтjabr oktobra oktobar october nocas danas sutra "
            "ulaznice besplatno ucastie meroprijatie vreme gde kogda budet etot etoj".split())


class PublicDiscoveryLead(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def outbound_urls(self) -> tuple[str, ...]: ...


@dataclass(slots=True)
class PublicSearchBudget:
    """One shared bounded budget across all public discovery layers in a cycle."""

    limit: int = 4
    claimed: set[tuple[str, str]] = field(default_factory=set)

    def claim(self, source_id: str, url: str) -> bool:
        key = (source_id, url)
        if key in self.claimed or len(self.claimed) >= self.limit:
            return False
        self.claimed.add(key)
        return True


@dataclass(frozen=True, slots=True)
class PrimaryResolution:
    status: str
    primary_results: tuple[str, ...]
    primary_urls: tuple[str, ...]
    searches_performed: int
    discovery_gaps: tuple[str, ...] = ()


ResolveLink = Callable[[str], Awaitable[str | None]]
SearchPlan = Callable[[str], list[tuple[str, str]]]
SearchOfficial = Callable[[str, str], Awaitable[list[RawItem]]]
AcceptOfficial = Callable[[RawItem], bool]
RegisterOfficial = Callable[[RawItem], None]


def folded(text: str) -> str:
    text = "".join(_LETTERS.get(c, c) for c in text.casefold())
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def topic_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", folded(text[:8000])) if w not in _STOP}


def topic_matches(text: str, title: str) -> bool:
    return len(topic_words(text) & topic_words(title)) >= 3


def source_for_url(url: str, sources: dict[str, SourceConfig]) -> str | None:
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            return None
        if "city_beograd_news" in sources and match_beograd_article_url(url):
            return "city_beograd_news"
        if TRAFFIC_SECRETARIAT_SOURCE in sources and match_bgsaobracaj_notice(url):
            return TRAFFIC_SECRETARIAT_SOURCE
        if ("cukarica_news" in sources and parts.hostname in {"cukarica.rs", "www.cukarica.rs"}
                and ARTICLE_PATH.fullmatch(parts.path)):
            return "cukarica_news"
        for key, source in sources.items():
            if source.source_type != "official_venue_events":
                continue
            try:
                _allowed(url, source)
            except ValueError:
                continue
            return key
        # A link from a channel to an article of a verified outlet becomes that outlet's item.
        # Homepages and section pages (fewer than two path segments) are not articles.
        if AVIATION_SOURCE in sources and aviation_article_id(url):
            return AVIATION_SOURCE
        for key, source in sources.items():
            if source.source_type != "news_media" or key == AVIATION_SOURCE:
                continue
            try:
                _allowed(url, source)
            except ValueError:
                continue
            if len([part for part in parts.path.split("/") if part]) >= 2:
                return key
    except ValueError:
        pass
    return None


def public_search_plan(
    text: str, sources: dict[str, SourceConfig], aliases: dict[str, list[str]],
) -> list[tuple[str, str]]:
    """Only PUBLIC discovery text may be supplied. Private tips use explicit URLs/local matching."""
    normalized = folded(text[:8000])
    result = []
    for key, names in aliases.items():
        source = sources.get(key)
        if source is None or not source.extraction.feed_url:
            continue
        if not any(re.search(r"(?<!\w)" + re.escape(folded(name)) + r"(?!\w)", normalized)
                   for name in names):
            continue
        excluded = topic_words(" ".join(names))
        words = [w for w in re.findall(r"[^\W\d_]{4,}", text) if
                 folded(w) in topic_words(text) - excluded]
        # One explicit term, never the whole post or an arbitrary supplied URL.
        if not words:
            continue
        term = max(words, key=len)[:60]
        parts = urlsplit(source.extraction.feed_url)
        query = dict(parse_qsl(parts.query))
        query["s"] = term
        result.append((key, urlunsplit(parts._replace(query=urlencode(query)))))
    return result[:2]


def _matches_article(text: str, article: RawItem) -> bool:
    if article.source_id != AVIATION_SOURCE:
        return topic_matches(text, article.title)
    carriers = city_carriers("belgrade")
    found = [_carriers_in(value, carriers) for value in (text[:8000], article.title)]
    if not any(found):
        return topic_matches(text, article.title)
    return _same_carrier_report(text, article.title)


# Даты не различают направления: одинаковый месяц или число не делают новость той же.
_MONTH_PREFIXES = ("janu", "janv", "febr", "fevr", "mart", "apri", "aprel", "maj", "maja",
                   "jun", "juna", "ijun", "jul", "jula", "ijul", "avgu", "sept", "sent",
                   "okto", "oktj", "nove", "noja", "dece", "deka")


def _is_date_term(term: str) -> bool:
    return term.isdigit() or term.startswith(_MONTH_PREFIXES)


def _carriers_in(value: str, carriers: Mapping[str, re.Pattern[str]]) -> set[str]:
    return {carrier for carrier, pattern in carriers.items() if pattern.search(value)}


def _same_carrier_report(lead: str, title: str, *, city: str = "belgrade") -> bool:
    """Один перевозчик из реестра города, общее направление и ещё один общий признак.

    Названия в разных алфавитах сравниваются по сходству транслитерации, без списков замен.
    Дата и общие слова об отмене без совпадающего направления недостаточны.
    """
    carriers = city_carriers(city)
    found = [_carriers_in(value, carriers) for value in (lead[:8000], title)]
    if len(found[0]) != 1 or found[0] != found[1]:
        return False
    pattern = carriers[next(iter(found[0]))]

    def terms(value: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{4,}|\d{1,4}", folded(pattern.sub(" ", value)))
                if w not in _STOP}

    lead_terms = terms(lead[:8000])
    shared: list[str] = []
    for term in terms(title):
        if term in lead_terms or (len(term) >= 5 and not term.isdigit() and any(
                len(other) >= 5 and SequenceMatcher(None, term, other).ratio() >= 0.75
                for other in lead_terms)):
            shared.append(term)
    names = [term for term in shared if not _is_date_term(term)]
    # Направление: общее название из одного алфавита в другой или из того же,
    # но не служебное слово об отмене, которое есть почти в каждой авиановости.
    return len(shared) >= 2 and any(term not in _AVIATION_WORDS for term in names)


_AVIATION_WORDS = frozenset({
    "letove", "letova", "letovi", "ukida", "ukinut", "obustavlja", "otkazuje", "otkazan",
    "rejsy", "rejsov", "otmenjaet", "otmenila", "otmenil", "aviakompanija", "flights",
    "kompanija", "linija", "liniju", "beograd", "beograda", "belgrade", "belgrada",
})


async def resolve_public_discovery(
    lead: PublicDiscoveryLead,
    *,
    articles: list[RawItem],
    resolve_link: ResolveLink,
    search_plan: SearchPlan,
    search_official: SearchOfficial,
    accept_official: AcceptOfficial,
    register_official: RegisterOfficial,
    search_budget: PublicSearchBudget,
) -> PrimaryResolution:
    """Resolve a public discovery hint only through independently fetched official evidence.

    The lead text and URL never become evidence. Direct outbound URLs must first survive the
    caller's authoritative source allowlist. Topic similarity can only point at an already
    collected official item or at a bounded search on an already approved official source.
    """
    results: list[str] = []
    primary_urls: list[str] = []
    media_urls: list[str] = []
    searches_performed = 0
    gaps: list[str] = []

    for url in lead.outbound_urls[:10]:
        result = await resolve_link(url)
        if result is None:
            gaps.append("outbound_url_not_supported")
            continue
        if aviation_article_id(url):
            if result in {"primary_source_collected", "already_collected"}:
                media_urls.append(url)
            continue
        results.append(result)
        primary_urls.append(url)

    # This is a pointer to independently collected evidence, never claim confirmation.
    for article in tuple(articles):
        if not _matches_article(lead.text, article) or article.url in primary_urls:
            continue
        if article.source_id == AVIATION_SOURCE:
            media_urls.append(article.url)
            continue
        primary_urls.append(article.url)
        results.append("primary_source_collected")

    if not any(result == "primary_source_collected" for result in results):
        plan = search_plan(lead.text)
        if not plan:
            gaps.append("no_configured_search_plan")
        for source_id, search_url in plan:
            if not search_budget.claim(source_id, search_url):
                gaps.append("search_already_attempted" if (source_id, search_url)
                            in search_budget.claimed else "search_budget_exhausted")
                continue
            searches_performed += 1
            found = await search_official(source_id, search_url)
            accepted = False
            for item in found:
                if not topic_matches(lead.text, item.title) or not accept_official(item):
                    continue
                register_official(item)
                primary_urls.append(item.url)
                results.append("primary_source_collected")
                accepted = True
            if not accepted:
                gaps.append("search_without_accepted_match")

    resolved = any(
        result in {"primary_source_collected", "already_collected"}
        for result in results
    )
    status = ("primary_source_resolved" if resolved else
              "media_source_for_editor_review" if media_urls else "deferred_missing_primary_source")
    return PrimaryResolution(
        status=status,
        primary_results=tuple(results) + (("attributed_media",) if media_urls else ()),
        primary_urls=tuple(dict.fromkeys([*primary_urls, *media_urls])),
        searches_performed=searches_performed,
        # Только коды причин. Не расширять allowlist и не сохранять тексты/URL
        # отклонённых ссылок ради диагностики.
        discovery_gaps=tuple(dict.fromkeys(gaps)) if not resolved else (),
    )
