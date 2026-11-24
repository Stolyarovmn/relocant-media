"""Подтверждение датированного анонса карточкой организатора без даты публикации."""

import json
import re
from datetime import date
from urllib.parse import urlsplit

from relocant_media.collectors.base import RawItem as CollectedArticle
from relocant_media.collectors.linked_tour_map import SOURCE_ID as TOUR_MAP_SOURCE
from relocant_media.collectors.linked_tour_map import STRATEGY as TOUR_MAP_STRATEGY
from relocant_media.collectors.linked_tour_map import cited_tour_link, performers
from relocant_media.collectors.ticket_vision_events import SOURCE_ID as TICKET_VISION_SOURCE
from relocant_media.collectors.ticket_vision_events import STRATEGY as TICKET_VISION_STRATEGY
from relocant_media.collectors.ticket_vision_events import cited_ticket_urls
from relocant_media.domain.city_profile import DEFAULT_CITIES_ROOT
from relocant_media.domain.event_facts import event_dates
from relocant_media.domain.originality import copies_source_text
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.primary_discovery import folded, topic_words
from relocant_media.sources.registry import SourceConfig, load_source_file
from relocant_media.storage.models import RawItem, Source

_STRUCTURED = frozenset({
    "bitefartcafe_events", "yugoslavia_programs", TICKET_VISION_STRATEGY, TOUR_MAP_STRATEGY,
})
_LEAD_LIMIT = 2500
_CHANGED = re.compile(
    r"\b(?:otkaz|odloz|pomer|cancel|postpon|reschedul|otmen|perenos)\w*"
)
_EVENT_SENTENCE = re.compile(r"\b(?:koncert|nastup|festival|izlozb|predstav)\w*")


def is_undated_venue_source(config: SourceConfig) -> bool:
    return (
        config.extraction.strategy in _STRUCTURED
        and config.extraction.status == "active"
        and config.source_type == "official_venue_events"
        and config.ownership == "official"
        and not config.policy.auto_publish
        and not config.policy.high_risk
    )


def _named_venue(lead: str, aliases: list[str]) -> bool:
    return any(alias and re.search(r"(?<!\w)" + re.escape(folded(alias)) + r"(?!\w)", lead)
               for alias in aliases)


def named_venue_sources(item: RawItem, source: Source) -> dict[str, SourceConfig]:
    if (source.ownership != "media" or source.source_type != "news_media"
            or source.high_risk or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source.city)):
        return {}
    city = DEFAULT_CITIES_ROOT / source.city
    path = city / "event-aliases.json"
    aliases = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    lead = folded(f"{item.title}\n{item.body[:_LEAD_LIMIT]}")
    result: dict[str, SourceConfig] = {}
    ticket_path = city / "sources" / f"{TICKET_VISION_SOURCE}.yaml"
    if len(cited_ticket_urls(item.body)) == 1 and ticket_path.is_file():
        config = load_source_file(ticket_path)
        if (config.id == TICKET_VISION_SOURCE
                and config.extraction.strategy == TICKET_VISION_STRATEGY
                and is_undated_venue_source(config)):
            result[config.id] = config
    tour_path = city / "sources" / f"{TOUR_MAP_SOURCE}.yaml"
    if tour_path.is_file() and cited_tour_link(item.body, city) is not None:
        config = load_source_file(tour_path)
        if (config.id == TOUR_MAP_SOURCE and config.extraction.strategy == TOUR_MAP_STRATEGY
                and is_undated_venue_source(config)):
            result[config.id] = config
    for source_id, names in aliases.items():
        if not _named_venue(lead, names):
            continue
        source_path = city / "sources" / f"{source_id}.yaml"
        if source_path.is_file():
            config = load_source_file(source_path)
            if config.id == source_id and is_undated_venue_source(config):
                result[source_id] = config
    return result


def matches_venue_evidence(
    media: RawItem | CollectedArticle, venue: RawItem | CollectedArticle, config: SourceConfig,
    *, published: date,
) -> bool:
    if (media.published_at is None or venue.published_at is not None
            or not is_public_https_url(venue.url)
            or urlsplit(venue.url).hostname not in config.extraction.approved_hosts):
        return False
    if config.extraction.strategy == TICKET_VISION_STRATEGY:
        return _matches_cited_ticket(media, venue, published=published)
    if config.extraction.strategy == TOUR_MAP_STRATEGY:
        return _matches_cited_tour_map(media, venue, published=published)
    media_title, venue_title = folded(media.title), folded(venue.title)
    shared_title = (topic_words(media_title) & topic_words(venue_title)) - topic_words(config.name)
    if len(shared_title) < 2 or not copies_source_text(media_title, (venue_title,), run_words=3):
        return False
    media_lead = folded(media.body[:_LEAD_LIMIT])
    venue_lead = folded(venue.body[:_LEAD_LIMIT])
    if any(_CHANGED.search(text) for text in (media_title, venue_title, media_lead, venue_lead)):
        return False
    days = event_dates(venue_lead, published.year)
    if len(days) != 1:
        return False
    try:
        event_day = date.fromisoformat(next(iter(days)))
    except ValueError:
        return False
    if event_day < published:
        return False
    dates_in_media = {day for day in event_dates(media_lead, published.year)
                      if day >= published.isoformat()}
    if dates_in_media - {published.isoformat()} != days - {published.isoformat()}:
        return False
    if event_day == published:
        # If both dates coincide, a publication timestamp does not prove the event date.
        return any(days <= event_dates(line, published.year)
                   and copies_source_text(line, (venue_lead,), run_words=12)
                   for line in media_lead.splitlines())
    return copies_source_text(media_lead, (venue_lead,), run_words=12)


def _matches_cited_ticket(
    media: RawItem | CollectedArticle, ticket: RawItem | CollectedArticle, *, published: date,
) -> bool:
    if cited_ticket_urls(media.body) != {ticket.url}:
        return False
    title = topic_words(ticket.title)
    lead = folded(media.title + "\n" + media.body[:_LEAD_LIMIT])
    if len(title) < 2 or not title <= topic_words(lead):
        return False
    venue = re.search(r"^Mesto: (.+), Beograd\.$", ticket.body, re.M)
    if venue is None:
        return False
    venue_name = folded(venue[1])
    first, separator, rest = venue_name.partition(" ")
    names = [venue_name]
    if separator and len(first) >= 4 and first.endswith("a"):
        # A Serbian feminine first word may be declined while the rest stays exact.
        names.extend(f"{first[:-1]}{ending} {rest}" for ending in ("e", "i", "u", "om"))
    if not _named_venue(lead, names):
        return False
    return _matches_cited_date(lead, ticket.body, published=published)


def _matches_cited_tour_map(
    media: RawItem | CollectedArticle, ticket: RawItem | CollectedArticle, *, published: date,
) -> bool:
    link = cited_tour_link(media.body, DEFAULT_CITIES_ROOT / "belgrade")
    if link is None or ticket.url != link.map_ticket:
        return False
    artists = performers(folded(ticket.title))
    lead = folded(media.title + "\n" + media.body[:_LEAD_LIMIT])
    if len(artists) < 2 or not artists <= topic_words(lead):
        return False
    venue = re.search(r"^Mesto: (.+), Beograd\.$", ticket.body, re.M)
    if venue is None:
        return False
    name = folded(venue[1])
    # "Hangar Luka Beograd" is also written "u Hangaru Luke Beograd" in Serbian.
    # Preserve the whole venue name even when the head noun and its modifier are declined.
    parts = name.split()
    pattern = r"\s+".join(
        re.escape(part[:-1]) + "[aeiu]" if index < len(parts) - 1 and part.endswith("a")
        else re.escape(part) + "u?" if index == 0 and not part.endswith("a")
        else re.escape(part)
        for index, part in enumerate(parts)
    )
    return bool(re.search(r"(?<!\w)" + pattern + r"(?!\w)", lead)) and _matches_cited_date(
        lead, ticket.body, published=published,
    )


def _matches_cited_date(lead: str, body: str, *, published: date) -> bool:
    dates = event_dates(body, published.year)
    if len(dates) != 1 or _CHANGED.search(lead):
        return False
    article_day, event_day = published.isoformat(), next(iter(dates))
    media_dates = {day for day in event_dates(lead, published.year) if day > article_day}
    if event_day != article_day:
        return dates == media_dates
    return not media_dates and any(
        _EVENT_SENTENCE.search(sentence) and event_dates(sentence, published.year) == dates
        for sentence in re.split(r"[\n;!?]|(?<=[a-z])\.\s+", lead)
    )
