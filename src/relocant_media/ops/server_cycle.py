"""Bounded server newsroom; reviewed packages precede any enabled channel delivery."""
import argparse
import asyncio
import copy
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from relocant_media.ai.gemini_process import IsolatedGeminiProvider
from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.package import EditorialPackage
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.ai.public_cache import PublicPreparationCache
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.collectors.beograd_article import (
    BelgradeArticleCollector,
    parse_beograd_article,
)
from relocant_media.collectors.beograd_news import parse_beograd_news_list
from relocant_media.collectors.cukarica_news import (
    CukaricaNewsCollector,
    parse_cukarica_article,
    parse_cukarica_news_list,
)
from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.news_discovery import (
    collect_news_feed,
    load_verified_news_feeds,
)
from relocant_media.collectors.telegram_topics import (
    DEFAULT_CHANNELS as DEFAULT_CHANNELS,
)
from relocant_media.collectors.telegram_topics import (
    collect_topics,
    messenger_sources_for_city,
    runtime_channels_for_city,
)
from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.domain.attention import source_priorities, venue_attention
from relocant_media.domain.dedup import event_key
from relocant_media.domain.freshness import freshness_decision
from relocant_media.domain.presentation import (
    presentation_title,
    title_instruction,
    valid_title_presentation,
)
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops.cycle_audit import audit_cycle
from relocant_media.ops.newsroom_status import summarize
from relocant_media.ops.newsroom_triage import triage_queue
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.services.audience_observation import observe_audience
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import (
    DeliveryBlockedError,
    EditorialService,
    _candidate,
    _document,
    _event_items,
    _snapshot_member,
)
from relocant_media.services.event_eligibility import event_content_reason
from relocant_media.services.package_import import import_package
from relocant_media.services.pin_policy import maintain_pins
from relocant_media.services.policy_reassessment import (
    eligible_empty_policy_hold,
    persist_policy_reassessment,
)
from relocant_media.services.primary_discovery import (
    PublicSearchBudget,
    public_search_plan,
    resolve_public_discovery,
    source_for_url,
    topic_matches,
)
from relocant_media.services.private_intake import receive_private_tips
from relocant_media.services.published_watch import (
    evidence_changed,
    published_sources,
    recent_source_load,
)
from relocant_media.services.submission_feedback import reply_to_submissions, resolve_submission
from relocant_media.services.submission_queue import (
    batch_submissions,
    pending_submissions,
    priority_raw_ids,
    tip_urls,
)
from relocant_media.sources.registry import SourceConfig, load_source_file, load_source_registry
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source
from relocant_media.storage.sqlite_schema import verify_sqlite_schema

SOURCES = Path("cities/belgrade/sources")
NEWS_DISCOVERY = Path("docs/serbian-news-discovery.json")
_SERVER_VENUE_STRATEGIES = frozenset({
    "venue_feed",
    "musicology_blog",
    "natural_history_news",
})
_REVIEW_ONLY_VENUE_STRATEGIES = frozenset({
    "bitefartcafe_events",
    "yugoslavia_programs",
})


class ServerAI:
    """Keep server roles Russian and bind relevance decisions to the actual date."""
    def __init__(self, provider: StructuredAIProvider, *, audit_path: Path | None = None,
                 provider_name: str = "unknown", model_id: str | None = None):
        self.provider = provider
        self.audit_path = audit_path
        self.provider_name = provider_name
        self.model_id = model_id

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        policy = ("Все пояснения, причины, оценки и тексты пиши по-русски. "
                  "Сербское написание сохраняй только в собственных именах. "
                  "Все предложения основного текста переводи на русский, включая цитаты; "
                  "нельзя копировать сербский текст claims в русский черновик. "
                  f"Текущий момент UTC: {datetime.now(UTC).isoformat()}. "
                  "Отличай дату статьи от срока события: истёкшие объявления не выбирай "
                  "для новой публикации и не выдавай планы за выполненные факты. "
                  "Завтра/sutra и сегодня/danas считай от даты публикации в Белграде; "
                  "в русском черновике указывай абсолютную дату, а не перенесённое завтра. "
                  "Официальный сайт площадки подтверждает её собственный анонс, "
                  "а не независимую истинность рекламных обещаний. Для events проверь "
                  "дату, время, место, стоимость или условия покупки/бесплатного входа; "
                  "язык и возрастные ограничения укажи, если они важны и известны. "
                  "Не выдумывай отсутствующие детали, не выдавай медицинские обещания "
                  "и обычные скидки/пакеты проживания за городское событие. ")
        policy += title_instruction()
        if task == "rank":
            categories = ["transport", "events", "city", "utilities", "schools", "medicine",
                          "health", "immigration", "law", "tax", "banking", "politics",
                          "crime", "accusations", "real_estate", "jobs"]
            policy += ("category — машинный идентификатор на английском из разрешённого списка. "
                       "Культурные анонсы и афиша относятся к events; движение транспорта — "
                       "к transport. Не переводить category на русский и не маскировать "
                       "медицинские, правовые, политические и иные рискованные темы как events. ")
            response_schema = copy.deepcopy(response_schema)
            if "category" in response_schema.get("properties", {}):
                response_schema["properties"]["category"]["enum"] = categories
        started = time.monotonic()
        record: dict[str, Any] = {"task": task, "provider": self.provider_name,
            "model": self.model_id, "occurred_at": datetime.now(UTC).isoformat()}
        try:
            response = self.provider.generate_json(
                task=task, system_instruction=policy + system_instruction,
                prompt=prompt, response_schema=response_schema)
            record["status"] = "completed"
            return response
        except Exception as error:
            record.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            if self.audit_path is not None:
                with self.audit_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")



class _CachedOutcome(EditorialPipeline):
    def __init__(self, outcome: EditorialOutcome, documents: tuple[EvidenceDocument, ...]):
        self.outcome, self.documents = outcome, documents

    def run(self, *, city: str, documents: tuple[EvidenceDocument, ...]) -> EditorialOutcome:
        if city != "belgrade" or documents != self.documents:
            raise ValueError("evidence changed before persistence")
        return self.outcome


def venue_sources() -> dict[str, SourceConfig]:
    """Active venue sources safe for the dated autonomous editorial queue."""
    return {
        source.id: source
        for source in load_source_registry(SOURCES)
        if source.extraction.strategy in _SERVER_VENUE_STRATEGIES
        and source.extraction.status == "active"
        and source.source_type == "official_venue_events"
    }


def review_only_venue_sources() -> dict[str, SourceConfig]:
    """Verified undated venue sources retained only for deterministic human review."""
    return {
        source.id: source
        for source in load_source_registry(SOURCES)
        if source.extraction.strategy in _REVIEW_ONLY_VENUE_STRATEGIES
        and source.extraction.status == "active"
        and source.source_type == "official_venue_events"
        and not source.policy.auto_publish
    }


def _is_review_only_venue_hold(item: Any, *, now: datetime) -> bool:
    if event_content_reason(item.title, item.body, now=now) is not None:
        return False
    fresh = freshness_decision(item.title, item.body, item.published_at, now)
    return fresh.action == "hold" and fresh.reason == "publication_date_unknown"


def discovery_venue_sources() -> dict[str, SourceConfig]:
    """Legacy search-enabled venue feeds; structured list collectors are not search APIs."""
    return {
        source.id: source
        for source in load_source_registry(SOURCES)
        if source.extraction.strategy == "venue_feed"
        and source.extraction.status == "active"
        and source.source_type == "official_venue_events"
    }


async def _collect_exact_url(collector: Any, url: str) -> Any:
    collect_url = getattr(collector, "collect_url", None)
    if callable(collect_url):
        return await collect_url(url)
    matches = [
        item
        for item in await collector.collect()
        if item.url == url or item.canonical_url == url
    ]
    if len(matches) != 1:
        raise ValueError("official source item unavailable or ambiguous")
    return matches[0]


def fetch_documents(package: EditorialPackage) -> tuple[EvidenceDocument, ...]:
    assert package.event is not None
    documents = []
    venues = venue_sources()
    async def fetch_venue(source: SourceConfig, url: str) -> Any:
        async with httpx.AsyncClient(timeout=25, trust_env=False) as venue_client:
            collector = build_collector(source, venue_client)
            return await _collect_exact_url(collector, url)
    with httpx.Client(timeout=25, trust_env=False) as client:
        for document in package.event.documents:
            if document.source_id in venues:
                item = asyncio.run(fetch_venue(venues[document.source_id], document.url))
                documents.append(EvidenceDocument(source_id=item.source_id, url=item.url,
                    title=item.title, body=item.body, published_at=item.published_at))
                continue
            if document.source_id not in {"city_beograd_news", "cukarica_news"}:
                raise ValueError("unsupported live source")
            source = load_source_file(SOURCES / f"{document.source_id}.yaml")
            host = "beograd.rs" if document.source_id == "city_beograd_news" else "cukarica.rs"
            parts = urlsplit(document.url)
            if (parts.scheme != "https" or parts.hostname not in {host, "www." + host}
                    or parts.username is not None or parts.password is not None
                    or parts.port not in {None, 443}):
                raise ValueError("live source URL is not approved")
            response = client.get(document.url, follow_redirects=False)
            response.raise_for_status()
            parser = (parse_beograd_article if document.source_id == "city_beograd_news"
                      else parse_cukarica_article)
            item = parser(response.text, source, str(response.url))
            documents.append(EvidenceDocument(source_id=item.source_id, url=item.url,
                title=item.title, body=item.body, published_at=item.published_at))
    return tuple(documents)


async def collect_sources(
    engine: Engine, *, output: Path | None = None, submissions_only: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    result: dict[str, Any] = {"sources_synced": 2, "sources_skipped": 0, "raw_items_written": 0,
              "failures": 0, "review_required": 0, "discarded_before_article_fetch": 0,
              "discarded_before_persistence": 0, "channel_failures": 0, "channel_leads": 0,
              "news_failures": 0, "news_leads": 0}
    venues = venue_sources()
    review_venues = review_only_venue_sources()
    runtime_venues = {**venues, **review_venues}
    discovery_venues = discovery_venue_sources()
    news_feeds = () if submissions_only else load_verified_news_feeds(NEWS_DISCOVERY)
    sources = {key: load_source_file(SOURCES / f"{key}.yaml")
               for key in ("city_beograd_news", "cukarica_news")}
    sources.update(runtime_venues)
    result["sources_synced"] = len(sources)
    result["venue_sources"] = len(venues)
    result["venue_review_only_sources"] = len(review_venues)
    result["venue_items"] = 0
    result["venue_deferred"] = 0
    result["venue_review_holds"] = 0
    result["venue_article_failures"] = 0
    result["news_feeds"] = len(news_feeds)
    result["priority_submissions"] = 0
    result["primary_searches"] = 0
    result["private_link_fetches"] = 0
    with Session(engine) as session:
        for source in sources.values():
            sync_source(session, "belgrade", source)
        session.commit()
    attention_leads = []
    topics: list[dict[str, Any]] = []
    news_topics: list[dict[str, Any]] = []
    channel_errors: list[dict[str, str]] = []
    channel_coverage: list[dict[str, Any]] = []
    news_errors: list[dict[str, str]] = []
    venue_errors: list[dict[str, str]] = []
    articles = []
    review_holds = []
    review_hold_urls: set[str] = set()
    fetched: dict[str, str] = {}

    def register_review_hold(item: Any) -> None:
        if item.url not in review_hold_urls:
            review_holds.append(item)
            review_hold_urls.add(item.url)
            result["venue_review_holds"] += 1
        fetched[item.url] = "primary_source_review_hold"
    submission_links: dict[int, list[str]] = {}
    attempted_tips: set[int] = set()
    watch: dict[str, Any] = {"checked": 0, "changed": [], "failures": []}
    with Session(engine) as session:
        rows = batch_submissions(session) if submissions_only else pending_submissions(session)
        tips = [(row.id, row.payload) for row in rows]
        published = [] if submissions_only else published_sources(session, now=now)
    result["submission_ids"] = [sid for sid, _ in tips]
    result["submission_raw_ids"] = []
    aliases_path = SOURCES.parent / "event-aliases.json"
    aliases = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.is_file() else {}
    search_budget = PublicSearchBudget()
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        collectors: dict[str, Any] = {
            "city_beograd_news": BelgradeArticleCollector(sources["city_beograd_news"], client),
            "cukarica_news": CukaricaNewsCollector(sources["cukarica_news"], client),
        }

        collectors.update({
            key: build_collector(source, client) for key, source in runtime_venues.items()
        })

        async def full_article(source_id: str, url: str) -> str:
            if url in fetched:
                return fetched[url]
            item = await _collect_exact_url(collectors[source_id], url)
            fresh = freshness_decision(item.title, item.body, item.published_at, now)
            venue_reason = (event_content_reason(item.title, item.body, now=now)
                            if source_id in runtime_venues else None)
            if (source_id in review_venues and venue_reason is None
                    and _is_review_only_venue_hold(item, now=now)):
                register_review_hold(item)
                return fetched[url]
            if fresh.action != "keep" or venue_reason is not None:
                result["discarded_before_persistence"] += 1
                fetched[url] = venue_reason or fresh.reason
                return fetched[url]
            articles.append(item)
            fetched[url] = "primary_source_collected"
            return "primary_source_collected"

        async def resolve_link(url: str) -> str | None:
            source_id = source_for_url(url, sources)
            if source_id is None:
                return None
            try:
                return await full_article(source_id, url)
            except (httpx.HTTPError, ValueError):
                fetched[url] = "primary_source_unavailable"
                return fetched[url]

        def plan_public_searches(text: str) -> list[tuple[str, str]]:
            return public_search_plan(text, discovery_venues, aliases)

        async def search_official(source_id: str, search_url: str) -> list[Any]:
            search_source = discovery_venues[source_id].model_copy(update={"extraction":
                discovery_venues[source_id].extraction.model_copy(update={"feed_url": search_url})})
            try:
                return await VenueFeedCollector(search_source, client).collect()
            except (httpx.HTTPError, ValueError):
                return []

        def accept_official(item: Any) -> bool:
            return (
                freshness_decision(item.title, item.body, item.published_at, now).action == "keep"
                and event_content_reason(item.title, item.body, now=now) is None
            )

        def register_official(item: Any) -> None:
            if item.url not in fetched:
                articles.append(item)
                fetched[item.url] = "primary_source_collected"

        # Private text stays in Submission. Only separately fetched public evidence is ingested.
        for submission_id, payload in tips:
            linked = []
            for url in tip_urls(payload):
                if source_for_url(url, sources) is None:
                    attempted_tips.add(submission_id)
                    continue
                if urlsplit(url).query:
                    attempted_tips.add(submission_id)
                    continue
                if url not in fetched:
                    if result["private_link_fetches"] >= 10:
                        continue
                    result["private_link_fetches"] += 1
                attempted_tips.add(submission_id)
                if await resolve_link(url) in {
                    "primary_source_collected", "primary_source_review_hold"
                }:
                    linked.append(url)
            if not tip_urls(payload):
                attempted_tips.add(submission_id)
            submission_links[submission_id] = linked

        for original in published:
            if original.source_id not in collectors:
                continue
            try:
                current = await _collect_exact_url(collectors[original.source_id], original.url)
                watch["checked"] += 1
                if evidence_changed(original, current):
                    articles.append(current)
                    watch["changed"].append(original.id)
            except (httpx.HTTPError, ValueError) as error:
                watch["failures"].append({"raw_item_id": original.id,
                                          "error_type": type(error).__name__})

        pages = [("city_beograd_news", str(sources["city_beograd_news"].url)),
                 ("cukarica_news", str(sources["cukarica_news"].url)),
                 ("city_beograd_news",
                  "https://www.beograd.rs/lat/servisne-informacije/saobracaj-i-prevoz")]
        if submissions_only:
            pages = []
        for source_id, url in pages:
            try:
                page = await client.get(url, follow_redirects=False)
                page.raise_for_status()
                parser = (parse_beograd_news_list if source_id == "city_beograd_news"
                          else parse_cukarica_news_list)
                links = parser(page.text, sources[source_id])
                if not links or len(links) > 100:
                    raise ValueError("unexpected source list")
                for item in links[:50]:
                    # Cards expose the date before downloading any full text.
                    # Up to seven days allows a dated ongoing closure to survive;
                    # the full-text freshness gate then enforces the 48h news rule.
                    if (item.published_at is None or item.published_at > now
                            or item.published_at < now - timedelta(days=7)):
                        result["discarded_before_article_fetch"] += 1
                        continue
                    await full_article(source_id, item.url)
            except Exception:
                result["failures"] += 1
        for source_id in (() if submissions_only else runtime_venues):
            collector = collectors[source_id]
            try:
                for item in await collector.collect():
                    result["venue_items"] += 1
                    reason = event_content_reason(item.title, item.body, now=now)
                    fresh = freshness_decision(item.title, item.body, item.published_at, now)
                    if (source_id in review_venues and reason is None
                            and _is_review_only_venue_hold(item, now=now)):
                        result["venue_deferred"] += 1
                        register_review_hold(item)
                        continue
                    if reason is not None or fresh.action != "keep":
                        result["venue_deferred"] += 1
                        continue
                    articles.append(item)
                    fetched[item.url] = "primary_source_collected"
            except Exception:
                result["failures"] += 1
            finally:
                item_failures = getattr(collector, "item_failures", [])
                result["venue_article_failures"] += len(item_failures)
                venue_errors.extend({"source_id": source_id, **error}
                                    for error in item_failures)
        runtime_channels = () if submissions_only else runtime_channels_for_city("belgrade")
        for channel in runtime_channels:
            observation: dict[str, Any] = {"channel": channel, "status": "not_checked"}
            channel_coverage.append(observation)
            try:
                leads = await collect_topics(channel, client, now=now, observation=observation)
                attention_leads.extend(leads)
                for lead in leads[:20]:
                    result["channel_leads"] += 1
                    resolution = await resolve_public_discovery(
                        lead,
                        articles=articles,
                        resolve_link=resolve_link,
                        search_plan=plan_public_searches,
                        search_official=search_official,
                        accept_official=accept_official,
                        register_official=register_official,
                        search_budget=search_budget,
                    )
                    result["primary_searches"] += resolution.searches_performed
                    topics.append({"url": lead.url, "channel": channel,
                        "published_at": lead.published_at.isoformat(),
                        "status": resolution.status,
                        "primary_results": list(resolution.primary_results),
                        "primary_urls": list(resolution.primary_urls), "ai_calls": 0})
            except Exception as error:
                result["channel_failures"] += 1
                channel_errors.append({"channel": channel, "error_type": type(error).__name__})
                if observation["status"] == "not_checked":
                    observation["status"] = "access_error"

        for feed in news_feeds:
            try:
                news_leads = await collect_news_feed(feed, client, now=now)
                for news_lead in news_leads[:20]:
                    result["news_leads"] += 1
                    resolution = await resolve_public_discovery(
                        news_lead,
                        articles=articles,
                        resolve_link=resolve_link,
                        search_plan=plan_public_searches,
                        search_official=search_official,
                        accept_official=accept_official,
                        register_official=register_official,
                        search_budget=search_budget,
                    )
                    result["primary_searches"] += resolution.searches_performed
                    news_topics.append({"url": news_lead.url,
                        "source_id": news_lead.source_id,
                        "published_at": news_lead.published_at.isoformat(),
                        "status": resolution.status,
                        "primary_results": list(resolution.primary_results),
                        "primary_urls": list(resolution.primary_urls), "ai_calls": 0})
            except Exception as error:
                result["news_failures"] += 1
                news_errors.append({"source_id": feed.source_id,
                                    "error_type": type(error).__name__})
    with Session(engine) as session:
        written, changes = capture_collected_batch(session, [*articles, *review_holds])
        for submission_id, payload in tips:
            urls = submission_links[submission_id]
            urls.extend(url for url in tip_urls(payload)
                        if fetched.get(url) == "primary_source_collected")
            # Local matching sends no private text to a search engine or model.
            for article in articles:
                if topic_matches(str(payload.get("text", "")), article.title):
                    urls.append(article.url)
            ids = list(session.scalars(select(RawItem.id).where(RawItem.url.in_(set(urls)))))
            if not ids and submission_id not in attempted_tips:
                continue
            from relocant_media.storage.models import Submission
            stored_tip = session.get(Submission, submission_id)
            assert stored_tip is not None
            resolve_submission(stored_tip, ids, now=now)
            if ids and session.scalar(select(EditorialRun.raw_item_id).where(
                    EditorialRun.raw_item_id.in_(ids)).limit(1)) is not None:
                stored_tip.payload = {**stored_tip.payload, "triage_reason": "already_considered"}
            result["priority_submissions"] += bool(ids)
            result["submission_raw_ids"].extend(ids)
        session.commit()
    result["raw_items_written"] = written
    result["review_required"] = changes
    triage = triage_queue(engine, now=now)
    result["discarded_queue"] = triage["discarded"]
    if output is not None:
        (output / "published-source-watch.json").write_text(
            json.dumps(watch, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "venue-article-failures.json").write_text(
            json.dumps({"items": venue_errors}, ensure_ascii=False, indent=2), encoding="utf-8")
        aliases_path = SOURCES.parent / "event-aliases.json"
        aliases = (
            json.loads(aliases_path.read_text(encoding="utf-8"))
            if aliases_path.is_file() else {}
        )
        attention = venue_attention(attention_leads, aliases)
        (output / "venue-attention.json").write_text(
            json.dumps(attention, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "triage.json").write_text(json.dumps(triage, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        blocked = [{"source_id": row["id"], "platform": row["platform"],
                    "status": ("blocked_no_message_access"
                               if row["platform"] in {"viber", "whatsapp"} else "catalog_only")}
                   for row in messenger_sources_for_city("belgrade")
                   if row.get("collection", {}).get("status") != "runtime_public_preview"]
        (output / "messenger-coverage.json").write_text(json.dumps({
            "observed_at": now.isoformat(), "city": "belgrade",
            "scope": "submissions_only" if submissions_only else "editorial_discovery",
            "interval_coverage": "not_checked" if submissions_only else "partial",
            "sources": channel_coverage + blocked,
            "publication_evidence": False,
            "web_fallback_is_messenger_access": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "channel-topics.json").write_text(json.dumps({"topics": topics,
            "failures": channel_errors, "publication_evidence": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
        (output / "news-topics.json").write_text(json.dumps({"topics": news_topics,
            "failures": news_errors, "publication_evidence": False,
            "requires_primary_verification": True}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return result


def prepare_reviewed(
    engine: Engine, raw_id: int, provider: StructuredAIProvider, output: Path,
) -> dict[str, Any]:
    with Session(engine) as session:
        raw = session.get(RawItem, raw_id)
        assert raw is not None
        source = session.get(Source, raw.source_id)
        assert source is not None
        existing = session.get(EditorialRun, raw_id)
        reassessing = existing is not None and eligible_empty_policy_hold(session, raw_id)
        if existing is not None and not reassessing:
            return {"raw_item_id": raw_id, "status": existing.outcome["status"],
                    "reason": existing.outcome.get("reason", "already_processed")}
        members = _event_items(session, anchor=raw, anchor_source=source)
        documents = tuple(_document(item) for item, _ in members)
        snapshots = [_snapshot_member(item, policy) for item, policy in members]
        identity = dict(raw_item_id=raw.id, source_id=raw.source_id, article_id=raw.external_id)
        key = event_key(_candidate(raw))
    freshness = freshness_decision(title=raw.title, body=raw.body,
        published_at=raw.published_at, now=datetime.now(UTC))
    venue_reason = (event_content_reason(raw.title, raw.body, now=datetime.now(UTC))
                    if source.source_type == "official_venue_events" else None)
    if freshness.action != "keep" or venue_reason is not None:
        from relocant_media.ai.models import RankingResult
        outcome = EditorialOutcome(
            status=(PublicationStatus.SKIPPED if freshness.action == "discard"
                    or venue_reason is not None else PublicationStatus.HUMAN_REVIEW),
            reason=venue_reason or freshness.reason,
            ranking=RankingResult(should_process=False, relevance=0, category="city",
                confidence=0, rationale=venue_reason or freshness.reason), confidence=0)
    else:
        # Source publication policy is enforced on import and delivery, separately
        # from autonomous editorial assessment. Never turn a source gate into an
        # unexamined request for the owner to read every article.
        outcome = EditorialPipeline(provider).run(city="belgrade", documents=documents)
    if outcome.status != PublicationStatus.READY or outcome.draft is None:
        with Session(engine) as session:
            run = (persist_policy_reassessment(session, raw_id, outcome, documents)
                   if reassessing else
                   EditorialService(session, _CachedOutcome(outcome, documents)).prepare(raw_id))
            return {"raw_item_id": raw_id, "status": run.outcome["status"],
                    "reason": run.outcome["reason"],
                    "resolution": "deferred_policy" if run.outcome["status"] == "human_review"
                                  else "rejected"}
    draft = outcome.draft.model_copy(update={
        "title": presentation_title(outcome.draft.title),
    })
    outcome = outcome.model_copy(update={"draft": draft})
    placeholders = [dict(reviewer_id=f"pending-{role}", role=role,
        source_id=identity["source_id"], article_id=identity["article_id"],
        context_sha256="0" * 64, draft_sha256="0" * 64, reviewed_at=datetime.now(UTC).isoformat(),
        verdict="hold", score=0, flags=["pending_review"], live_source_checked=False)
        for role in ("facts", "utility")]
    package = EditorialPackage.model_validate_json(json.dumps(dict(
        version=2, policy_version="subscription-editorial-v2", city="belgrade", **identity,
        urgent=outcome.ranking.category == "transport",
        evidence=documents[0].model_dump(mode="json"),
        event=dict(event_key=key, editorial_rule_version="belgrade-editorial-v2",
                   documents=snapshots), outcome=outcome.model_dump(mode="json"),
        reviews=placeholders)))
    audit: list[dict[str, Any]] = []
    try:
        package = review_with_repairs(package, provider, lambda: fetch_documents(package),
                                   audit=audit.append)
    finally:
        (output / f"review-audit-{raw_id}.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    with Session(engine) as session:
        run = import_package(session, package, replace_empty_hold=reassessing)
        result = {"raw_item_id": raw_id, "status": run.outcome["status"],
                  "reason": run.outcome["reason"],
                  "resolution": "ready" if run.outcome["status"] == "ready" else "deferred_policy",
                  "review_scores": [r.score for r in package.reviews]}
    (output / f"package-{raw_id}.json").write_text(
        package.model_dump_json(indent=2), encoding="utf-8")
    return result


def policy_reassessment_ids(engine: Engine) -> list[int]:
    with Session(engine) as session:
        ids = session.scalars(select(EditorialRun.raw_item_id).join(RawItem).join(Source)
            .where(Source.city == "belgrade").order_by(RawItem.published_at.desc()))
        return [i for i in ids if eligible_empty_policy_hold(session, i)]


class ChannelPublisher:
    """Target cannot be chosen by a model, source, package or CLI argument."""
    def __init__(self, bot: TelegramBotAPI):
        self.bot = bot

    async def publish(self, text: str) -> int:
        if not valid_title_presentation(text.split("\n", 1)[0]):
            raise ValueError("title presentation does not match channel mode")
        return await self.bot.send_message(APPROVED_CHANNEL_ID, text)


async def cleanup_news_pins(engine: Engine) -> dict[str, Any]:
    policy = json.loads((SOURCES.parent / "pin-policy.json").read_text(encoding="utf-8"))
    with Session(engine) as session:
        news_ids = set(session.scalars(select(Post.telegram_message_id).join(
            EditorialRun, EditorialRun.post_id == Post.id).where(
                Post.city == "belgrade", EditorialRun.delivery_status == "sent",
                Post.telegram_message_id.is_not(None))))
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        return await maintain_pins(TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client),
                                   policy, news_message_ids={i for i in news_ids if i is not None})


async def audience_observation() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        return await observe_audience(TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client))


async def publish_pending(
    engine: Engine, *, enabled: bool, limit: int = 2,
    blocked: list[dict[str, Any]] | None = None,
    only_raw_ids: set[int] | None = None,
) -> list[int]:
    if not enabled or only_raw_ids == set():
        return []
    with Session(engine) as session:
        query = select(EditorialRun.raw_item_id).where(
            EditorialRun.delivery_status == "pending",
        )
        if only_raw_ids is not None:
            query = query.where(EditorialRun.raw_item_id.in_(only_raw_ids))
        ids = list(session.scalars(query.order_by(EditorialRun.raw_item_id)))
    sent: list[int] = []
    async with httpx.AsyncClient(timeout=30, trust_env=False,
                                  transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        publisher = ChannelPublisher(TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client))
        for raw_id in ids:
            if len(sent) >= limit:
                break
            with Session(engine) as session:
                run = session.get(EditorialRun, raw_id)
                assert run is not None
                if run.outcome.get("status") != "ready" or "_package" not in run.outcome:
                    continue
                try:
                    package = EditorialPackage.model_validate_json(
                        json.dumps(run.outcome["_package"]))
                except ValueError:
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id, "reason": "invalid_stored_package"})
                    continue
                if package.version != 2:
                    continue
                assert package.event is not None
                item = session.get(RawItem, raw_id)
                assert item is not None
                fresh = freshness_decision(title=item.title, body=item.body,
                    published_at=item.published_at, now=datetime.now(UTC))
                if fresh.action != "keep":
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id, "reason": fresh.reason})
                    continue
                # Independent reviews and one final exact live-source comparison
                # must all remain valid before the durable send claim.
                try:
                    live = await asyncio.to_thread(fetch_documents, package)
                    if any(a.model_dump() != b.model_dump(
                            include=set(EvidenceDocument.model_fields))
                           for a, b in zip(live, package.event.documents, strict=True)):
                        raise ValueError("live source changed before delivery")
                except (ValueError, httpx.HTTPError) as error:
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id,
                                        "reason": "live_source_check_failed",
                                        "error_type": type(error).__name__})
                    continue
                try:
                    result = await deliver_reviewed(session, raw_id, publisher,
                        target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
                except DeliveryBlockedError as error:
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id, "reason": "delivery_guard_blocked",
                                        "error_type": type(error).__name__})
                    continue
                assert result.message_id is not None
                sent.append(result.message_id)
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="Серверная редакция с двумя ревью и квитанциями")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--submissions-only", action="store_true")
    args = parser.parse_args()
    engine = None
    try:
        url = make_url(os.environ["DATABASE_URL"])
        if (url.get_backend_name() != "sqlite" or not url.database
                or not Path(url.database).is_file() or url.query):
            raise ValueError("existing local SQLite required")
        engine = create_engine(url)
        verify_sqlite_schema(engine)
        provider = PublicPreparationCache(
            ServerAI(IsolatedGeminiProvider(os.environ["GEMINI_API_KEY"],
                model=os.environ["GEMINI_MODEL"]),
                audit_path=args.output / "ai-calls.jsonl", provider_name="gemini",
                model_id=os.environ["GEMINI_MODEL"]),
            Path(url.database).parent / "ai-cache",
            model_id=os.environ["GEMINI_MODEL"] + ":server-policy-v7-local-venue-style",
            public_news_only=True,
        )
        async def intake() -> dict[str, Any]:
            if os.environ.get("EDITORIAL_INTAKE_MODE") == "batch":
                return dict(status="batch_collector", received=0, owner_action_required=False)
            async with httpx.AsyncClient(timeout=25, trust_env=False) as intake_client:
                return await receive_private_tips(engine,
                    TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], intake_client),
                    enabled=os.environ.get("EDITORIAL_PRIVATE_INTAKE_ENABLED") == "true")
        try:
            intake_status = asyncio.run(intake())
        except Exception as intake_error:
            intake_status = {"status": "failed", "error_type": type(intake_error).__name__,
                             "received": 0, "owner_action_required": False}
        def private_priorities() -> set[int]:
            with Session(engine) as private_session:
                return priority_raw_ids(private_session)
        def source_load() -> dict[str, int]:
            with Session(engine) as load_session:
                return recent_source_load(load_session, now=datetime.now(UTC))
        scope: dict[str, Any] = {}
        def ingest() -> dict[str, Any]:
            stats = asyncio.run(collect_sources(engine, output=args.output,
                                               submissions_only=args.submissions_only))
            scope.update(stats)
            return stats
        def allowed_items() -> set[int]:
            return set(scope.get("submission_raw_ids", []))
        result = run_cycle(engine, city="belgrade", output=args.output, now=datetime.now(UTC),
            ingest=ingest,
            process=lambda raw_id: prepare_reviewed(engine, raw_id, provider, args.output), limit=2,
            refresh_until=True, newest_first=True, allow_partial_ingest=True,
            priority_items=private_priorities,
            recent_source_counts=source_load,
            reassessment_ids=(None if args.submissions_only
                              else lambda: policy_reassessment_ids(engine)),
            continue_on_error=True, submissions_only=args.submissions_only,
            allowed_items=allowed_items if args.submissions_only else None,
            candidate_priorities=lambda: source_priorities(json.loads(
                (args.output / "venue-attention.json").read_text(encoding="utf-8")))
                if (args.output / "venue-attention.json").is_file() else {})
        result["private_intake"] = intake_status
        result["submissions_only"] = args.submissions_only
        async def feedback() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=25, trust_env=False) as feedback_client:
                return await reply_to_submissions(engine,
                    TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], feedback_client),
                    enabled=os.environ.get("EDITORIAL_PRIVATE_FEEDBACK_ENABLED") == "true",
                    only_ids=(set(scope.get("submission_ids", []))
                              if args.submissions_only else None))
        try:
            result["private_feedback"] = asyncio.run(feedback())
        except Exception as feedback_error:
            result["private_feedback"] = {"status": "failed",
                "error_type": type(feedback_error).__name__}
        result["fallback_required"] = (result["status"] == "PRIMARY_FAILED"
                                       or bool(result.get("processing_errors")))
        result["sent_message_ids"] = []
        result["channel_transport"] = "disabled"
        if result["status"] in {"PRIMARY_OK", "PRIMARY_PARTIAL"}:
            enabled = os.environ.get("EDITORIAL_SEND_ENABLED") == "true"
            result["channel_transport"] = "bot-api" if enabled else "disabled"
            blocked: list[dict[str, Any]] = []
            result["sent_message_ids"] = asyncio.run(
                publish_pending(engine, enabled=enabled, blocked=blocked,
                    only_raw_ids=allowed_items() if args.submissions_only else None))
            result["channel_blocked"] = blocked
            result["fallback_required"] = result["fallback_required"] or bool(blocked)
            if enabled:
                result["pin_maintenance"] = asyncio.run(cleanup_news_pins(engine))
                audience = asyncio.run(audience_observation())
                (args.output / "audience-observation.json").write_text(
                    json.dumps(audience, ensure_ascii=False, indent=2), encoding="utf-8")
                result["audience_observation"] = audience
            with Session(engine) as audit_session:
                cycle_checks = audit_cycle(audit_session, result)
            (args.output / "cycle-audit.json").write_text(
                json.dumps(cycle_checks, ensure_ascii=False, indent=2), encoding="utf-8")
            result["cycle_audit_status"] = cycle_checks["status"]
    except Exception as error:
        result = {"status": "SERVER_FAILED", "error_type": type(error).__name__,
                  "fallback_required": True}
    finally:
        if engine is not None:
            engine.dispose()
    if args.output.exists():
        try:
            database = make_url(os.environ["DATABASE_URL"]).database
            assert database is not None
            snapshot = create_snapshot(Path(database), args.output / "snapshot")
            result.update(backup_ready=True, backup_sha256=snapshot["sha256"],
                          remote_backup="pending")
        except Exception as error:
            result.update(status="SERVER_FAILED", backup_error_type=type(error).__name__)
        (args.output / "server-report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.output.exists():
        (args.output / "newsroom-summary.json").write_text(
            json.dumps(summarize(args.output), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] not in {"PRIMARY_OK", "PRIMARY_PARTIAL"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
