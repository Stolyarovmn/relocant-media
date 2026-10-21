"""Bounded primary-source discovery; topic similarity never grants factual approval."""
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from relocant_media.collectors.beograd_article import match_beograd_article_url
from relocant_media.collectors.cukarica_news import ARTICLE_PATH
from relocant_media.collectors.venue_feed import _allowed
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
    except ValueError:
        pass
    return None


def public_search_plan(
    text: str, sources: dict[str, SourceConfig], aliases: dict[str, list[str]],
) -> list[tuple[str, str]]:
    """Only PUBLIC channel text may be supplied. Private tips use explicit URLs/local matching."""
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
