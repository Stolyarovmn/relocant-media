"""Найти доступное официальное подтверждение для недоступной темы."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedArticle
from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import canonicalize_url
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.editorial import _candidate
from relocant_media.services.primary_discovery import public_search_plan, source_for_url
from relocant_media.services.replacement_identity import matches_replacement_event
from relocant_media.services.source_replacement import (
    SourceReplacementError,
    record_source_replacement,
)
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import EditorialRun, RawItem, Source

FetchArticle = Callable[[SourceConfig, str, httpx.AsyncClient], Awaitable[CollectedArticle]]
_LINK = re.compile(r'https://[^\s<>"]+')


def _stored_urls(session: Session, original: RawItem) -> list[str]:
    urls = [match.rstrip(").,;:»") for match in _LINK.findall(original.body[:20_000])]
    snapshot = session.get(RawItemMediaSnapshot, original.id)
    if snapshot is not None and snapshot.source_html:
        soup = BeautifulSoup(snapshot.source_html, "html.parser")
        for anchor in soup.select("article a[href], main a[href]", limit=40):
            href = anchor.get("href")
            if isinstance(href, str):
                urls.append(href)
    return urls


async def recover_primary_source(
    engine: Engine, raw_id: int, *, sources: tuple[SourceConfig, ...],
    fetch_article: FetchArticle,
) -> int | None:
    """Один ограниченный поиск после фактически недоступного исходника."""
    now = datetime.now(UTC)
    official = {
        config.id: config for config in sources if config.extraction.status == "active"
        and config.ownership == "official" and config.policy.auto_publish
        and not config.policy.high_risk
    }
    with Session(engine) as session:
        original = session.get(RawItem, raw_id)
        source = session.get(Source, original.source_id) if original else None
        if (original is None or source is None or not source.enabled or not source.auto_publish
                or source.high_risk or source.ownership not in {"official", "media"}
                or session.get(EditorialRun, raw_id) is not None):
            return None
        profile = load_city_profile(source.city)
        anchor = _candidate(original)
        text = f"{original.title}\n{original.body[:8000]}"
        rows = session.scalars(select(RawItem).join(Source).where(
            Source.city == source.city, Source.enabled.is_(True),
            RawItem.source_id.in_(official), RawItem.id != raw_id,
            RawItem.published_at >= now - timedelta(days=7),
            RawItem.published_at <= now,
        ).order_by(RawItem.published_at.desc()).limit(150))
        urls = [item.url for item in rows if matches_replacement_event(anchor, _candidate(item))]
        urls.extend(_stored_urls(session, original))
    known: dict[str, tuple[SourceConfig, str]] = {}
    for url in urls:
        if not is_public_https_url(url):
            continue
        key = canonicalize_url(url)
        if key == anchor.resolved_canonical_url:
            continue
        source_id = source_for_url(url, official)
        if source_id is not None:
            known.setdefault(key, (official[source_id], url))
        if len(known) == 3:
            break

    def record(article: CollectedArticle, config: SourceConfig) -> int | None:
        with Session(engine) as session:
            try:
                new_id = record_source_replacement(
                    session, original=original, replacement=article, config=config,
                    now=datetime.now(UTC), profile=profile,
                )
                session.commit()
                return new_id
            except SourceReplacementError:
                return None

    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for config, url in known.values():
            try:
                async with asyncio.timeout(20):
                    article = await fetch_article(config, url, client)
            except (httpx.HTTPError, TimeoutError, ValueError):
                continue
            if canonicalize_url(article.url) == canonicalize_url(url):
                if (new_id := record(article, config)) is not None:
                    return new_id
        aliases_path = Path(f"cities/{profile.city_id}/event-aliases.json")
        aliases = (
            json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.is_file() else {}
        )
        feeds = {key: config for key, config in official.items()
                 if config.extraction.strategy == "venue_feed"}
        for source_id, url in public_search_plan(text, feeds, aliases):
            config = feeds[source_id]
            search = config.model_copy(update={"extraction": config.extraction.model_copy(
                update={"feed_url": url},
            )})
            try:
                async with asyncio.timeout(20):
                    articles = await VenueFeedCollector(search, client).collect()
            except (httpx.HTTPError, TimeoutError, ValueError):
                continue
            for article in articles:
                if (new_id := record(article, config)) is not None:
                    return new_id
    return None
