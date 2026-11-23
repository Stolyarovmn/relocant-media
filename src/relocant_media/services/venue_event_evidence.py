"""Подтверждение датированного анонса карточкой организатора без даты публикации."""

import json
import re
from datetime import date
from urllib.parse import urlsplit

from relocant_media.domain.city_profile import DEFAULT_CITIES_ROOT
from relocant_media.domain.event_facts import event_dates
from relocant_media.domain.originality import copies_source_text
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.primary_discovery import folded, topic_words
from relocant_media.sources.registry import SourceConfig, load_source_file
from relocant_media.storage.models import RawItem, Source

_STRUCTURED = frozenset({"bitefartcafe_events", "yugoslavia_programs"})
_LEAD_LIMIT = 2500
_CHANGED = re.compile(
    r"\b(?:otkaz|odloz|pomer|cancel|postpon|reschedul|otmen|perenos)\w*"
)


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
    if not path.is_file():
        return {}
    aliases = json.loads(path.read_text(encoding="utf-8"))
    lead = folded(f"{item.title}\n{item.body[:_LEAD_LIMIT]}")
    result: dict[str, SourceConfig] = {}
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
    media: RawItem, venue: RawItem, config: SourceConfig,
    *, published: date,
) -> bool:
    if (media.published_at is None or venue.published_at is not None
            or not is_public_https_url(venue.url)
            or urlsplit(venue.url).hostname not in config.extraction.approved_hosts):
        return False
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
