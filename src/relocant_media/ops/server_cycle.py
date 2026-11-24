"""Bounded server newsroom; reviewed packages precede any enabled channel delivery."""
import argparse
import asyncio
import copy
import json
import os
import time
from collections.abc import Coroutine, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import zip_longest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from relocant_media.ai.batch_preselection import preselect_candidates
from relocant_media.ai.diagnostics import AI_PHASES
from relocant_media.ai.gemini_process import IsolatedGeminiProvider
from relocant_media.ai.model_roles import RoleRoutedProvider, cache_model_id, role_models
from relocant_media.ai.models import (
    EditorialOutcome,
    EvidenceDocument,
    PublicationStatus,
    RankingResult,
)
from relocant_media.ai.package import EditorialPackage
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.ai.public_cache import PublicPreparationCache
from relocant_media.ai.quota_diagnostics import safe_quota
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.bot.formatting import (
    TELEGRAM_TEXT_LIMIT,
    editorial_message_html,
    formatted_text_units,
    rich_editorial_html,
)
from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.collectors.beograd_article import (
    UTILITY_SECTIONS,
    BelgradeArticleCollector,
    match_beograd_article_url,
    parse_beograd_article,
)
from relocant_media.collectors.beograd_news import parse_beograd_news_list
from relocant_media.collectors.bgprevoz_changes import BgprevozChangesCollector
from relocant_media.collectors.bgsaobracaj_notices import SOURCE_ID as TRAFFIC_SECRETARIAT_SOURCE
from relocant_media.collectors.cukarica_news import (
    CukaricaNewsCollector,
    parse_cukarica_article,
    parse_cukarica_news_list,
)
from relocant_media.collectors.factory import build_collector
from relocant_media.collectors.news_discovery import (
    NEWS_DISCOVERY_REGISTRY,
    collect_news_feed,
    load_verified_news_feeds,
)
from relocant_media.collectors.srbija_gov_article import match_srbija_gov_article_url
from relocant_media.collectors.telegram_topics import (
    DEFAULT_CHANNELS as DEFAULT_CHANNELS,
)
from relocant_media.collectors.telegram_topics import (
    TopicLead,
    collect_forwarded_topic,
    collect_topics,
    messenger_sources_for_city,
    runtime_channels_for_city,
)
from relocant_media.collectors.tls import source_ssl_context
from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.domain import quiet_hours
from relocant_media.domain.attention import source_priorities, venue_attention
from relocant_media.domain.city_profile import load_city_profile, load_queue_place_aliases
from relocant_media.domain.dedup import event_key, find_repeated_publication
from relocant_media.domain.flight_notices import FLIGHT_REVIEW_REASON
from relocant_media.domain.freshness import (
    GUIDE_SOURCE_TYPE,
    evidence_freshness,
    freshness_decision,
)
from relocant_media.domain.pacing import (
    PendingDelivery,
    delivery_order,
    morning_deadline,
    morning_release,
    pacing_hold,
)
from relocant_media.domain.policy import CURRENT_EDITORIAL_RULE_VERSION
from relocant_media.domain.presentation import (
    presentation_title,
    title_instruction,
    valid_title_presentation,
)
from relocant_media.domain.publication_time import same_legacy_publication_time
from relocant_media.domain.story_fingerprint import find_same_story
from relocant_media.ingestion.persistence import (
    capture_collected_batch,
    sync_source,
    upsert_media_snapshot,
)
from relocant_media.ops.collection_timing import measure_collection
from relocant_media.ops.cycle_audit import audit_cycle
from relocant_media.ops.newsroom_status import summarize
from relocant_media.ops.newsroom_triage import triage_queue
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.audience_observation import observe_audience
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.channel_presentation import ensure_map_pin_on_top
from relocant_media.services.editorial import (
    DeliveryBlockedError,
    EditorialService,
    PublishedTransportDuplicate,
    _candidate,
    _document,
    _event_items,
    _reviewed_event_item,
    _snapshot_member,
)
from relocant_media.services.editorial_products import (
    build_presentation,
    present_draft,
    recent_publications,
    rubric_plan,
    verify_live_map,
)
from relocant_media.services.event_eligibility import event_content_reason
from relocant_media.services.event_lifecycle import source_revision_lifecycle_context
from relocant_media.services.gpt_fallback import pending_gpt_ids
from relocant_media.services.kpi import record_subscriber_observation
from relocant_media.services.package_import import import_package
from relocant_media.services.pin_policy import maintain_pins
from relocant_media.services.policy_reassessment import (
    eligible_policy_reassessment,
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
from relocant_media.services.reader_forwarded_source import public_forward_urls, public_post_ref
from relocant_media.services.reader_publication import material_fingerprint, reader_credit
from relocant_media.services.reader_reports import (
    READER_REPORT_FIELD,
    is_reader_report_document,
    materialize_reader_reports,
    submission_id_for,
    with_reader_credit,
)
from relocant_media.services.reader_source_discovery import (
    CHECK_WINDOW,
    DISCOVERY_FIELD,
    confirmed_source_signal,
    matches_reader_event,
    pending_source_discovery,
    record_discovery_check,
    submission_time,
    undated_reader_match,
)
from relocant_media.services.source_enrichment import build_media_preparation_packet
from relocant_media.services.source_recovery import recover_primary_source
from relocant_media.services.source_replacement import SourceUnavailableError, is_source_unavailable
from relocant_media.services.submission_feedback import reply_to_submissions, resolve_submission
from relocant_media.services.submission_queue import (
    batch_submissions,
    pending_submissions,
    priority_raw_ids,
    tip_urls,
)
from relocant_media.services.transport_map import (
    MAP_MODE,
    transport_point,
)
from relocant_media.services.transport_map import (
    MAX_NOTICES as TRANSPORT_MAP_LIMIT,
)
from relocant_media.services.transport_map import (
    SOURCE_ID as TRANSPORT_MAP_SOURCE,
)
from relocant_media.services.transport_map_source import extract_transport_map
from relocant_media.services.utility_geography import resolve_power_points, resolve_utility_geometry
from relocant_media.services.utility_map import BVK_NEWS_SOURCE, utility_scope
from relocant_media.services.venue_event_evidence import is_undated_venue_source
from relocant_media.services.watched_updates import queue_watched_change
from relocant_media.services.water_outage_scope import extract_official_water_map
from relocant_media.sources.registry import SourceConfig, load_source_file, load_source_registry
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission
from relocant_media.storage.sqlite_schema import verify_sqlite_schema

SOURCES = Path("cities/belgrade/sources")
_SERVER_VENUE_STRATEGIES = frozenset({
    "venue_feed",
    "musicology_blog",
    "natural_history_news",
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
                  "Относительные даты считай от даты публикации в часовом поясе профиля; "
                  "в русском черновике указывай абсолютную дату, а не перенесённое завтра. "
                  "Официальный сайт площадки подтверждает её собственный анонс, "
                  "а не независимую истинность рекламных обещаний. Для events проверь "
                  "дату, время, место, стоимость или условия покупки/бесплатного входа; "
                  "язык и возрастные ограничения укажи, если они важны и известны. "
                  "Не выдумывай отсутствующие детали, не выдавай медицинские обещания "
                  "и обычные скидки/пакеты проживания за городское событие. ")
        policy += title_instruction()
        if task == "audit_rejection":
            policy += ("Исключение для evidence_refs: quote сохраняй дословно на языке "
                       "источника, иначе доказательство недействительно. Причину пиши по-русски. ")
        if task == "rank":
            profile = load_city_profile("belgrade")
            categories = sorted({category for rubric in profile.rubrics
                                 for category in rubric.categories})
            policy += ("category — машинный идентификатор на английском из разрешённого списка. "
                       "Анонсы для посетителей относятся к events, новости культурной жизни "
                       "без анонса — к culture, спортивные новости — к sport. Перекрытия и "
                       "движение транспорта относятся к transport, отключения воды и света — "
                       "к utilities. Не переводить category на русский и не маскировать "
                       "медицинские, правовые, политические и иные рискованные темы как events. "
                       "banking — только банковские услуги и регулирование для клиентов; "
                       "новости центрального банка, резервов, цен и курсов — economy. ")
            response_schema = copy.deepcopy(response_schema)
            if "category" in response_schema.get("properties", {}):
                response_schema["properties"]["category"]["enum"] = categories
        started = time.monotonic()
        model_for = getattr(self.provider, "model_for", None)
        record: dict[str, Any] = {"task": task, "provider": self.provider_name,
            "model": model_for(task) if callable(model_for) else self.model_id,
            "occurred_at": datetime.now(UTC).isoformat()}
        try:
            response = self.provider.generate_json(
                task=task, system_instruction=policy + system_instruction,
                prompt=prompt, response_schema=response_schema)
            record["status"] = "completed"
            return response
        except Exception as error:
            record.update(status="failed", error_type=type(error).__name__)
            phase = getattr(error, "phase", None)
            if phase in AI_PHASES:
                record["error_phase"] = phase
            quota = safe_quota(getattr(error, "quota", None))
            if quota:
                record["quota"] = quota
            raise
        finally:
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            if self.audit_path is not None:
                with self.audit_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")



class _CachedOutcome(EditorialPipeline):
    def __init__(self, outcome: EditorialOutcome, documents: tuple[EvidenceDocument, ...],
                 city: str = "belgrade"):
        self.outcome, self.documents = outcome, documents
        self.city = city

    def run(
        self, *, city: str, documents: tuple[EvidenceDocument, ...], evergreen: bool = False,
        reader_report: bool = False,
        recent_publications: list[dict[str, str]] | None = None,
        now: datetime | None = None, source_update: bool = False,
    ) -> EditorialOutcome:
        if city != self.city or documents != self.documents:
            raise ValueError("evidence changed before persistence")
        return self.outcome



def _aware(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)

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
        if is_undated_venue_source(source)
    }


# Candidates fully assessed per cycle. Ranking is one cheap call and most items are rejected
# there, so five per cycle drains the verified-media backlog without a second queue.
PROCESS_LIMIT = 5


def lifecycle_due_order(audit_ids: Iterable[int], retry_ids: Iterable[int]) -> list[int]:
    """Чередовать старшие ID повторов и аудита: оба пути получают место из обычных двух."""
    result: list[int] = []
    for pair in zip_longest(sorted(set(retry_ids)), sorted(set(audit_ids))):
        for raw_id in pair:
            if raw_id is not None and raw_id not in result:
                result.append(raw_id)
    return result


def queue_priorities(attention: dict[str, int], media: Iterable[str]) -> dict[str, int]:
    """Общие ленты СМИ идут ниже официальных; местные предупреждения повышает selector.

    Национальные потоки добавляют десятки сообщений в час. Исключение для местной
    практической темы меняет только порядок, не редакционное решение или допуск.
    """
    floor = min([0, *attention.values()]) - 1
    return {**attention, **{source_id: floor for source_id in media}}


def media_sources() -> dict[str, SourceConfig]:
    """Verified media feeds: full-text evidence on the normal news path (never high-risk)."""
    return {
        source.id: source
        for source in load_source_registry(SOURCES)
        if source.extraction.strategy in {"venue_feed", "aero_telegraf"}
        and source.extraction.status == "active"
        and source.source_type == "news_media"
        and source.ownership == "media"
        and not source.policy.high_risk
    }


def guide_sources() -> dict[str, SourceConfig]:
    """Official evergreen guides: valid for 30 days after first sight, owner queue only."""
    return {
        source.id: source
        for source in load_source_registry(SOURCES)
        if source.extraction.strategy == "official_guide"
        and source.extraction.status == "active"
        and source.source_type == GUIDE_SOURCE_TYPE
        and source.ownership == "official"
        and not source.policy.auto_publish
    }


def _is_review_only_venue_hold(item: Any, *, now: datetime) -> bool:
    if event_content_reason(item.title, item.body, now=now) is not None:
        return False
    fresh = freshness_decision(item.title, item.body, item.published_at, now,
                published_at_precision=item.published_at_precision)
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


async def fetch_public_article(
    config: SourceConfig, url: str, client: httpx.AsyncClient,
) -> CollectedRawItem:
    """Читать конкретный реестровый документ штатным разбором."""
    if config.id == "city_beograd_news":
        if match_beograd_article_url(url) is None:
            raise ValueError("unsupported live city article")
        response = await client.get(url, follow_redirects=False)
        response.raise_for_status()
        item = parse_beograd_article(response.text, config, str(response.url))
        return replace(item, source_html=response.text,
                       approved_image_hosts=("static.beograd.rs",))
    if config.id == "cukarica_news":
        return await CukaricaNewsCollector(config, client).collect_url(url)
    collector = build_collector(config, client)
    if isinstance(collector, VenueFeedCollector):
        return await collector.collect_url(url, require_page=True)
    item = await _collect_exact_url(collector, url)
    if not isinstance(item, CollectedRawItem):
        raise ValueError("live collector did not return an article")
    return item


@contextmanager
def _note_source_unavailable(source_id: str) -> Iterator[None]:
    try:
        yield
    except (httpx.HTTPError, TimeoutError, ValueError, ExceptionGroup) as error:
        if is_source_unavailable(error):
            raise SourceUnavailableError(source_id) from error
        raise


def _run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """asyncio.run, also from synchronous code that an async caller (the owner cycle) runs."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _live_document(original: EvidenceDocument, item: CollectedRawItem) -> EvidenceDocument:
    live = EvidenceDocument.model_validate(item, from_attributes=True)
    if same_legacy_publication_time(
        original.published_at, original.published_at_precision,
        live.published_at, live.published_at_precision,
    ):
        # Existing packages were signed with the old label. The full live-document
        # comparison still verifies source, URL, title, body and timestamp.
        return live.model_copy(update={"published_at_precision": original.published_at_precision})
    return live


def fetch_documents(package: EditorialPackage) -> tuple[EvidenceDocument, ...]:
    assert package.event is not None
    documents: list[EvidenceDocument] = []
    # Each source is re-read through the same parser used at intake.
    registered = {
        **venue_sources(), **review_only_venue_sources(), **media_sources(), **guide_sources(),
    }
    registered.update({
        key: load_source_file(SOURCES / f"{key}.yaml")
        for key in (BVK_NEWS_SOURCE, TRAFFIC_SECRETARIAT_SOURCE)
    })
    async def fetch_venue(source: SourceConfig, url: str) -> Any:
        async with httpx.AsyncClient(
            timeout=25, trust_env=False, verify=source_ssl_context(),
        ) as venue_client:
            return await fetch_public_article(source, url, venue_client)
    with httpx.Client(timeout=25, trust_env=False) as client:
        for document in package.event.documents:
            if is_reader_report_document(document.source_id):
                # No public page: delivery re-binds the document to the reader's material.
                documents.append(EvidenceDocument.model_validate(
                    document.model_dump(include=set(EvidenceDocument.model_fields))))
                continue
            if document.source_id in registered:
                with _note_source_unavailable(document.source_id):
                    item = _run_sync(fetch_venue(registered[document.source_id], document.url))
                documents.append(_live_document(document, item))
                verify_live_map(package.presentation, documents[-1], item.source_html)
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
            with _note_source_unavailable(document.source_id):
                response = client.get(document.url, follow_redirects=False)
                response.raise_for_status()
            parser = (parse_beograd_article if document.source_id == "city_beograd_news"
                      else parse_cukarica_article)
            item = parser(response.text, source, str(response.url))
            documents.append(_live_document(document, item))
            verify_live_map(package.presentation, documents[-1], response.text)
    native = package.presentation.native_map if package.presentation else None
    point = native.get("point", {}) if native else {}
    if point.get("mode") == MAP_MODE:
        primary = next((doc for doc in documents if doc.url == point["source_url"]), None)
        if primary is None:
            raise ValueError("transport map primary document is missing")
        config = load_source_file(SOURCES / f"{TRANSPORT_MAP_SOURCE}.yaml")
        notice = _run_sync(fetch_venue(config, point["notice_url"]))
        notice_document = EvidenceDocument.model_validate(notice, from_attributes=True)
        if transport_point(primary, notice_document, notice.source_html or "") != point:
            raise ValueError("official transport map changed in live source")
    return tuple(documents)


async def _enrich_utility_maps(
    engine: Engine, client: httpx.AsyncClient, articles: list[CollectedRawItem],
) -> int:
    attempted: set[str] = set()
    deadline = asyncio.get_running_loop().time() + 30
    for index, item in enumerate(articles):
        if item.source_html is None or item.source_id not in {"city_beograd_news", BVK_NEWS_SOURCE}:
            continue
        document = EvidenceDocument.model_validate(item, from_attributes=True)
        addresses, segments = utility_scope(document)
        if not (addresses or segments) or extract_official_water_map(document, item.source_html):
            continue
        category = "water" if segments else "power"
        with Session(engine) as session:
            stored = session.scalar(select(RawItem).where(
                RawItem.source_id == item.source_id, RawItem.external_id == item.external_id,
            ))
            if stored is not None and all(
                getattr(stored, field) == getattr(item, field)
                for field in ("url", "title", "body", "published_at", "published_at_precision")
            ):
                snapshot = session.get(RawItemMediaSnapshot, stored.id)
                if snapshot is not None and "utility_geography" in snapshot.manifest:
                    articles[index] = replace(
                        item, utility_geography=snapshot.manifest["utility_geography"],
                    )
                    continue
                if session.get(EditorialRun, stored.id) is not None:
                    continue
        remaining = deadline - asyncio.get_running_loop().time()
        if category in attempted or remaining <= 0:
            continue
        attempted.add(category)
        try:
            async with asyncio.timeout(min(remaining, 15)):
                points = await resolve_power_points(client, addresses) if addresses else []
                if points is None or segments:
                    geography = await resolve_utility_geometry(client, addresses, segments)
                else:
                    geography = {"points": points, "lines": []}
        except TimeoutError:
            continue
        if geography["points"] or geography["lines"]:
            articles[index] = replace(item, utility_geography=geography)
    return len(attempted)


async def collect_sources(
    engine: Engine, *, output: Path | None = None, submissions_only: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    collection_seconds: dict[str, float] = {}
    result: dict[str, Any] = {"sources_synced": 2, "sources_skipped": 0, "raw_items_written": 0,
              "failures": 0, "review_required": 0, "discarded_before_article_fetch": 0,
              "discarded_before_persistence": 0, "channel_failures": 0, "channel_leads": 0,
              "news_failures": 0, "news_leads": 0}
    venues = venue_sources()
    review_venues = review_only_venue_sources()
    runtime_venues = {**venues, **review_venues}
    # Intake still resolves explicit links to registered media; it skips feed discovery below.
    media = media_sources()
    guides = {} if submissions_only else guide_sources()
    discovery_venues = discovery_venue_sources()
    news_feeds = () if submissions_only else load_verified_news_feeds(NEWS_DISCOVERY_REGISTRY)
    sources = {key: load_source_file(SOURCES / f"{key}.yaml")
               for key in ("city_beograd_news", "cukarica_news", BVK_NEWS_SOURCE,
                           TRAFFIC_SECRETARIAT_SOURCE)}
    sources.update(runtime_venues)
    sources.update(media)
    sources.update(guides)
    result["sources_synced"] = len(sources)
    result["media_sources"] = len(media)
    result["media_items"] = 0
    result["media_deferred"] = 0
    result["guide_sources"] = len(guides)
    result["guide_items"] = 0
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
    news_feed_observations: list[dict[str, Any]] = []
    venue_errors: list[dict[str, str]] = []
    articles = []
    traffic_map_notices: list[CollectedRawItem] = []
    metadata_holds = []
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
    watch: dict[str, Any] = {"checked": 0, "changed": [], "queued": [], "failures": []}
    watched_changes: list[tuple[int, Any]] = []
    official_source_ids = {key for key, config in sources.items() if config.ownership == "official"}
    reader_candidate_sources = official_source_ids | media.keys()

    def reader_match(
        text: str, received: datetime, item: RawItem | CollectedRawItem,
    ) -> str | None:
        published_at = item.published_at
        if published_at is None:
            return None
        if item.source_id in official_source_ids and matches_reader_event(
            text, item.title, item.body, year=now.year, source_year=published_at.year,
        ):
            return "anchored"
        if item.source_id in reader_candidate_sources:
            return undated_reader_match(
                text, item.title, item.body, submitted_at=received, published_at=published_at,
            )
        return None

    def has_article_link(payload: dict[str, Any]) -> bool:
        return any(source_for_url(url, sources) or match_srbija_gov_article_url(url)
                   for url in tip_urls(payload))

    with Session(engine) as session:
        # Owner 23.11.2026: the reader's own permitted text becomes an attributed report.
        reader_report_ids = materialize_reader_reports(session, city="belgrade", now=now)
        session.commit()
    with Session(engine) as session:
        rows = batch_submissions(session) if submissions_only else pending_submissions(session)
        # A reader report already has its own document; link search must not reset it.
        tips = [(row.id, row.payload) for row in rows if READER_REPORT_FIELD not in row.payload]
        reader_pending = () if submissions_only else pending_source_discovery(
            session, city="belgrade", now=now, has_article_link=has_article_link,
        )
        reader_signals = {
            row.id: (str(row.payload.get("text", "")), submission_time(row),
                     material_fingerprint(row.payload)) for row in reader_pending
        }
        forwarded_tips: dict[tuple[str, int], set[int]] = {}
        for pending_tip in reader_pending:
            check = pending_tip.payload.get(DISCOVERY_FIELD, {})
            urls = public_forward_urls(pending_tip.payload)
            if (isinstance(check, dict) and check.get("status") == "forward_lookup_deferred"
                    and isinstance(check.get("remaining_forwards"), list)
                    and check.get("material_sha256") == material_fingerprint(pending_tip.payload)):
                urls = [url for url in urls if url in check["remaining_forwards"]]
            for url in urls:
                if ref := public_post_ref(url):
                    forwarded_tips.setdefault(ref, set()).add(pending_tip.id)
        recent_reader_articles = []
        if reader_signals:
            recent_reader_articles = list(session.scalars(select(RawItem).where(
                RawItem.source_id.in_(official_source_ids), RawItem.published_at <= now,
                RawItem.published_at >= now - timedelta(days=7),
            ).order_by(RawItem.published_at.desc(), RawItem.id.desc()).limit(150)))
            recent_reader_articles.extend(session.scalars(select(RawItem).where(
                RawItem.source_id.in_(media.keys()), RawItem.published_at <= now,
                RawItem.published_at >= now - CHECK_WINDOW,
            ).order_by(RawItem.published_at.desc(), RawItem.id.desc()).limit(100)))
        published = [] if submissions_only else published_sources(session, now=now)
    result["submission_ids"] = [sid for sid, _ in tips]
    result["submission_raw_ids"] = list(reader_report_ids)
    result["reader_reports"] = len(reader_report_ids)
    aliases_path = SOURCES.parent / "event-aliases.json"
    aliases = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.is_file() else {}
    search_budget = PublicSearchBudget()
    async with httpx.AsyncClient(
        timeout=25, trust_env=False, verify=source_ssl_context(),
    ) as client:
        collectors: dict[str, Any] = {
            "city_beograd_news": BelgradeArticleCollector(sources["city_beograd_news"], client),
            "cukarica_news": CukaricaNewsCollector(sources["cukarica_news"], client),
            BVK_NEWS_SOURCE: build_collector(sources[BVK_NEWS_SOURCE], client),
            TRAFFIC_SECRETARIAT_SOURCE: build_collector(
                sources[TRAFFIC_SECRETARIAT_SOURCE], client,
            ),
        }

        collectors.update({
            key: build_collector(source, client) for key, source in runtime_venues.items()
        })
        collectors.update({key: build_collector(source, client) for key, source in media.items()})
        collectors.update({key: build_collector(source, client) for key, source in guides.items()})

        async def full_article(source_id: str, url: str) -> str:
            if url in fetched:
                return fetched[url]
            item = await _collect_exact_url(collectors[source_id], url)
            fresh = freshness_decision(item.title, item.body, item.published_at, now,
                published_at_precision=item.published_at_precision)
            venue_reason = (event_content_reason(item.title, item.body, now=now)
                            if source_id in runtime_venues else None)
            if (source_id in review_venues and venue_reason is None
                    and _is_review_only_venue_hold(item, now=now)):
                register_review_hold(item)
                return fetched[url]
            if (fresh.action == "hold" and item.published_at_precision == "day"
                    and venue_reason is None):
                metadata_holds.append(item)
                fetched[url] = fresh.reason
                return fresh.reason
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
                freshness_decision(item.title, item.body, item.published_at, now,
                published_at_precision=item.published_at_precision).action == "keep"
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
                with measure_collection(collection_seconds, "reader_links"):
                    status = await resolve_link(url)
                if status in {
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
                with measure_collection(collection_seconds, "published_watch"):
                    current = await _collect_exact_url(collectors[original.source_id], original.url)
                watch["checked"] += 1
                if evidence_changed(original, current):
                    articles.append(current)
                    watch["changed"].append(original.id)
                    watched_changes.append((original.id, current))
            except (httpx.HTTPError, ValueError) as error:
                watch["failures"].append({"raw_item_id": original.id,
                                          "error_type": type(error).__name__})

        pages = [("city_beograd_news", str(sources["city_beograd_news"].url)),
                 ("cukarica_news", str(sources["cukarica_news"].url)),
                 ("city_beograd_news",
                  "https://www.beograd.rs/lat/servisne-informacije/saobracaj-i-prevoz")]
        utility_pages = {f"https://www.beograd.rs/lat/servisne-informacije/{section}"
                         for section in UTILITY_SECTIONS}
        pages.extend(("city_beograd_news", url) for url in sorted(utility_pages))
        if submissions_only:
            pages = []
        for source_id, url in pages:
            try:
                utility_page = url in utility_pages
                group = "city_pages" if source_id == "city_beograd_news" else "municipality_pages"
                with measure_collection(collection_seconds, group):
                    async with asyncio.timeout(20 if utility_page else None):
                        page = await client.get(url, follow_redirects=False)
                        page.raise_for_status()
                        parser = (parse_beograd_news_list if source_id == "city_beograd_news"
                                  else parse_cukarica_news_list)
                        links = parser(page.text, sources[source_id])
                        if not links or len(links) > 100:
                            raise ValueError("unexpected source list")
                        if utility_page:
                            prefix = f"{urlsplit(url).path}/"
                            links = [item for item in links
                                     if urlsplit(item.url).path.startswith(prefix)]
                        for item in links[:50]:
                            # Cards expose the date before downloading any full text.
                            # Up to seven days lets an ongoing dated closure survive.
                            if (item.published_at is None or item.published_at > now
                                    or item.published_at < now - timedelta(days=7)):
                                result["discarded_before_article_fetch"] += 1
                                continue
                            await full_article(source_id, item.url)
            except Exception:
                result["failures"] += 1
        if not submissions_only:
            try:
                async with asyncio.timeout(20):
                    with measure_collection(collection_seconds, "water_utility"):
                        water_notices = await collectors[BVK_NEWS_SOURCE].collect()
                    for item in water_notices:
                        if freshness_decision(
                            item.title, item.body, item.published_at, now,
                            published_at_precision=item.published_at_precision,
                        ).action == "keep":
                            register_official(item)
            except (TimeoutError, ValueError, httpx.HTTPError) as error:
                result["bvk_outage_source_error"] = type(error).__name__
            traffic = collectors[TRAFFIC_SECRETARIAT_SOURCE]
            try:
                async with asyncio.timeout(35):
                    with measure_collection(collection_seconds, "traffic_secretariat"):
                        notices = await traffic.collect()
                    result["traffic_secretariat_items"] = len(notices)
                    for item in notices:
                        fresh = freshness_decision(
                            item.title, item.body, item.published_at, now,
                            published_at_precision=item.published_at_precision,
                        )
                        if fresh.action == "keep":
                            register_official(item)
                        elif fresh.reason != "publication_date_future":
                            # The map uses official closure dates. Keep this evidence out of
                            # article discovery while retaining amendments to older notices.
                            traffic_map_notices.append(item)
                        else:
                            result["discarded_before_persistence"] += 1
            except (TimeoutError, ValueError, httpx.HTTPError) as error:
                result["traffic_secretariat_error"] = type(error).__name__
            result["traffic_secretariat_article_failures"] = len(
                getattr(traffic, "item_failures", []),
            )
        for source_id in (() if submissions_only else runtime_venues):
            collector = collectors[source_id]
            try:
                with measure_collection(collection_seconds, "venues"):
                    venue_notices = await collector.collect()
                for item in venue_notices:
                    result["venue_items"] += 1
                    reason = event_content_reason(item.title, item.body, now=now)
                    fresh = freshness_decision(item.title, item.body, item.published_at, now,
                published_at_precision=item.published_at_precision)
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
        for source_id in (() if submissions_only else media):
            try:
                with measure_collection(collection_seconds, "media"):
                    media_notices = await collectors[source_id].collect()
                for item in media_notices:
                    result["media_items"] += 1
                    if freshness_decision(item.title, item.body, item.published_at, now,
                            published_at_precision=item.published_at_precision
                            ).action != "keep":
                        result["media_deferred"] += 1
                        continue
                    articles.append(item)
            except Exception:
                result["failures"] += 1
        for source_id in guides:
            try:
                # Fetched now, so the page text is verified now: no date inference involved.
                with measure_collection(collection_seconds, "guides"):
                    guide_notices = await collectors[source_id].collect()
                for item in guide_notices:
                    result["guide_items"] += 1
                    articles.append(item)
            except Exception:
                result["failures"] += 1
        runtime_channels = () if submissions_only else runtime_channels_for_city("belgrade")
        allowed_channels = {channel.lower(): channel for channel in runtime_channels}

        async def fetch_channel(channel: str) -> tuple[list[Any], dict[str, Any]]:
            observation: dict[str, Any] = {"channel": channel, "status": "not_checked"}
            try:
                async with asyncio.timeout(20):
                    leads = await collect_topics(channel, client, now=now, observation=observation)
            except Exception as error:
                result["channel_failures"] += 1
                channel_errors.append({"channel": channel, "error_type": type(error).__name__})
                return [], observation
            return leads, observation

        fetched_channels = []
        for offset in range(0, len(runtime_channels), 6):
            with measure_collection(collection_seconds, "channel_previews"):
                fetched_channels.extend(await asyncio.gather(*(
                    fetch_channel(channel) for channel in runtime_channels[offset:offset + 6]
                )))
        # The private signal is compared locally to fetched posts. Only the separate,
        # publicly fetched post is passed to the existing source discovery service.
        channel_leads = []
        seen_forwards: set[tuple[str, int]] = set()
        forwarded_primary: dict[int, set[str]] = {}
        for channel, (leads, observation) in zip(runtime_channels, fetched_channels, strict=True):
            channel_coverage.append(observation)
            attention_leads.extend(leads)
            for lead in leads[:20]:
                ref = (channel.lower(), lead.message_id)
                if ref in forwarded_tips:
                    seen_forwards.add(ref)
                matches_reader = ref in forwarded_tips or any(matches_reader_event(
                    text, lead.text.partition("\n")[0], lead.text,
                    year=now.year, source_year=lead.published_at.year,
                ) for text, _, _ in reader_signals.values())
                channel_leads.append((matches_reader, channel, lead, observation))
        missing = [
            ref for ref in forwarded_tips if ref not in seen_forwards and ref[0] in allowed_channels
        ]
        remaining_forwards: dict[int, list[str]] = {}
        for ref in missing[4:]:
            for submission_id in forwarded_tips[ref]:
                remaining_forwards.setdefault(submission_id, []).append(
                    f"https://t.me/{ref[0]}/{ref[1]}",
                )
        with measure_collection(collection_seconds, "reader_links"):
            forward_lookups = await asyncio.gather(*(
                collect_forwarded_topic(allowed_channels[username], message_id, client, now=now)
                for username, message_id in missing[:4]
            ), return_exceptions=True)
        unavailable_forwards: set[int] = set()
        for ref, lead in zip(missing[:4], forward_lookups, strict=True):
            if isinstance(lead, TopicLead):
                channel_leads.append((True, allowed_channels[ref[0]], lead, {}))
            elif isinstance(lead, BaseException):
                unavailable_forwards.update(forwarded_tips[ref])
        for _, channel, lead, observation in sorted(channel_leads, key=lambda entry: not entry[0]):
            try:
                result["channel_leads"] += 1
                with measure_collection(collection_seconds, "channel_evidence"):
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
                if interested := forwarded_tips.get((channel.lower(), lead.message_id)):
                    cited = set(lead.outbound_urls) & set(resolution.primary_urls)
                    verified = {
                        url for url in cited if fetched.get(url) == "primary_source_collected"
                    }
                    for submission_id in interested:
                        forwarded_primary.setdefault(submission_id, set()).update(verified)
                result["primary_searches"] += resolution.searches_performed
                topics.append({"url": lead.url, "channel": channel,
                    "published_at": lead.published_at.isoformat(),
                    "status": resolution.status,
                    "primary_results": list(resolution.primary_results),
                    "discovery_gaps": list(resolution.discovery_gaps),
                    "primary_urls": list(resolution.primary_urls), "ai_calls": 0})
            except Exception as error:
                result["channel_failures"] += 1
                channel_errors.append({"channel": channel, "error_type": type(error).__name__})
                if observation["status"] == "not_checked":
                    observation["status"] = "access_error"

        for feed in news_feeds:
            observation = {"source_id": feed.source_id, "url": feed.feed_url,
                           "status": "not_checked"}
            news_feed_observations.append(observation)
            try:
                with measure_collection(collection_seconds, "news_feeds"):
                    news_leads = await collect_news_feed(
                        feed, client, now=now, observation=observation,
                    )
                for news_lead in news_leads[:20]:
                    result["news_leads"] += 1
                    with measure_collection(collection_seconds, "news_evidence"):
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
                        "discovery_gaps": list(resolution.discovery_gaps),
                        "primary_urls": list(resolution.primary_urls), "ai_calls": 0})
            except Exception as error:
                if observation["status"] == "not_checked":
                    observation["status"] = "error"
                result["news_failures"] += 1
                news_errors.append({"source_id": feed.source_id,
                                    "error_type": type(error).__name__})
        # A recent article can move off the feed. Confirm its saved public URL live again.
        previous_urls: dict[str, bool] = {}
        for saved_article in recent_reader_articles:
            if saved_article.url in fetched or saved_article.url in previous_urls:
                continue
            strengths = {reader_match(text, received, saved_article)
                         for text, received, _ in reader_signals.values()} - {None}
            if strengths:
                previous_urls[saved_article.url] = "anchored" in strengths
        with measure_collection(collection_seconds, "reader_links"):
            for url in sorted(previous_urls, key=previous_urls.__getitem__, reverse=True)[:4]:
                await resolve_link(url)
        with measure_collection(collection_seconds, "utility_maps"):
            result["utility_map_requests"] = await _enrich_utility_maps(engine, client, articles)
    transport_source = None
    transport_notices = []
    if not submissions_only:
        transport_source = load_source_file(SOURCES / f"{TRANSPORT_MAP_SOURCE}.yaml")
        try:
            async with asyncio.timeout(40), httpx.AsyncClient(
                timeout=12, trust_env=False, verify=source_ssl_context(),
            ) as transport_client:
                with measure_collection(collection_seconds, "transport_map"):
                    fetched_notices = await BgprevozChangesCollector(
                        transport_source, transport_client,
                    ).collect(limit=TRANSPORT_MAP_LIMIT)
            for notice in fetched_notices:
                official = EvidenceDocument.model_validate(notice, from_attributes=True)
                if extract_transport_map(official, notice.source_html or ""):
                    transport_notices.append(notice)
        except (TimeoutError, ValueError, httpx.HTTPError) as error:
            transport_notices = []
            result["transport_map_source_error"] = type(error).__name__
    result["transport_map_notices"] = len(transport_notices)
    result["traffic_secretariat_map_only"] = len(traffic_map_notices)
    with Session(engine) as session:
        if transport_notices:
            assert transport_source is not None
            sync_source(session, "belgrade", transport_source)
        written, changes = capture_collected_batch(
            session, [*articles, *review_holds, *metadata_holds,
                      *transport_notices, *traffic_map_notices])
        for original_id, live in watched_changes:
            parent = session.get(RawItem, original_id)
            if parent is not None and (new_id := queue_watched_change(session, parent, live)):
                upsert_media_snapshot(session, new_id, live)
                watch["queued"].append(new_id)
        for submission_id, payload in tips:
            urls = submission_links[submission_id]
            urls.extend(url for url in tip_urls(payload)
                        if fetched.get(url) == "primary_source_collected"
                        or any(item.url == url for item in metadata_holds))
            # Local matching sends no private text to a search engine or model.
            for article in articles:
                if topic_matches(str(payload.get("text", "")), article.title):
                    urls.append(article.url)
            ids = list(session.scalars(select(RawItem.id).where(RawItem.url.in_(set(urls)))))
            if not ids and submission_id not in attempted_tips:
                continue
            stored_tip = session.get(Submission, submission_id)
            assert stored_tip is not None
            resolve_submission(stored_tip, ids, now=now)
            if ids and session.scalar(select(EditorialRun.raw_item_id).where(
                    EditorialRun.raw_item_id.in_(ids)).limit(1)) is not None:
                stored_tip.payload = {**stored_tip.payload, "triage_reason": "already_considered"}
            result["priority_submissions"] += bool(ids)
            result["submission_raw_ids"].extend(ids)
        reader_checks: dict[str, int] = {"checked": 0, "linked": 0, "ambiguous": 0}
        for submission_id, (text, received, fingerprint) in reader_signals.items():
            row = session.get(Submission, submission_id, with_for_update=True)
            if (
                row is None or row.status != "reader_review"
                or not confirmed_source_signal(row.payload)
                or material_fingerprint(row.payload) != fingerprint
            ):
                continue
            exact_forwards = forwarded_primary.get(submission_id, set())
            matched_articles = {
                article.url: (article, strength) for article in articles
                if (strength := ("anchored" if article.url in exact_forwards else
                                 reader_match(text, received, article)))
            }
            candidates: dict[str, RawItem] = {}
            for saved_candidate in session.scalars(select(RawItem).where(
                RawItem.url.in_(matched_articles), RawItem.source_id.in_(reader_candidate_sources),
            ).order_by(RawItem.id.desc())):
                fresh_article, _ = matched_articles[saved_candidate.url]
                if (saved_candidate.source_id == fresh_article.source_id
                        and not evidence_changed(saved_candidate, fresh_article)):
                    candidates.setdefault(saved_candidate.url, saved_candidate)
            ordered = sorted(
                candidates.values(),
                key=lambda candidate: matched_articles[candidate.url][1] == "anchored",
                reverse=True,
            )
            unique_link = len(ordered) == 1 and matched_articles[ordered[0].url][1] == "anchored"
            remaining = tuple(remaining_forwards.get(submission_id, ())) if not unique_link else ()
            if remaining:
                status = "forward_lookup_deferred"
            elif unique_link:
                status = "linked"
            elif candidates:
                status = "candidate_review"
            elif submission_id in unavailable_forwards:
                status = "forward_source_unavailable"
            else:
                status = "no_match"
            record_discovery_check(
                row, now=now, status=status, remaining_forwards=remaining,
                candidates=tuple(ordered) if status == "candidate_review" else (),
            )
            reader_checks["checked"] += 1
            if unique_link:
                ids = [ordered[0].id]
                resolve_submission(row, ids, now=now)
                reader_checks["linked"] += 1
                result["priority_submissions"] += 1
                result["submission_raw_ids"].extend(ids)
            reader_checks["ambiguous"] += int(len(candidates) > 1)
        result["reader_source_discovery"] = reader_checks
        session.commit()
    result["raw_items_written"] = written
    result["metadata_holds"] = len(metadata_holds)
    result["review_required"] = changes
    with measure_collection(collection_seconds, "queue_triage"):
        triage = triage_queue(engine, now=now)
    result["discarded_queue"] = triage["discarded"]
    result["collection_seconds"] = collection_seconds
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
            "feeds": news_feed_observations, "failures": news_errors, "publication_evidence": False,
            "requires_primary_verification": True}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return result


def prepare_reviewed(
    engine: Engine, raw_id: int, provider: StructuredAIProvider, output: Path,
) -> dict[str, Any]:
    try:
        return _prepare_reviewed(engine, raw_id, provider, output)
    except SourceUnavailableError as error:
        with Session(engine) as session:
            original = session.get(RawItem, raw_id)
            if original is None or original.source_id != error.source_id:
                raise
        new_id = _run_sync(recover_primary_source(
            engine, raw_id, sources=load_source_registry(SOURCES),
            fetch_article=fetch_public_article,
        ))
        if new_id is None:
            raise
        result = _prepare_reviewed(engine, new_id, provider, output)
        return {**result, "replaced_unavailable_raw_item_id": raw_id}


def _prepare_reviewed(
    engine: Engine, raw_id: int, provider: StructuredAIProvider, output: Path,
) -> dict[str, Any]:
    with Session(engine) as session:
        raw = session.get(RawItem, raw_id)
        assert raw is not None
        source = session.get(Source, raw.source_id)
        assert source is not None
        profile = load_city_profile(source.city)
        existing = session.get(EditorialRun, raw_id)
        revision = source_revision_lifecycle_context(session, raw_id)
        reassessing = existing is not None and eligible_policy_reassessment(session, raw_id)
        if existing is not None and not reassessing:
            return {"raw_item_id": raw_id, "status": existing.outcome["status"],
                    "reason": existing.outcome.get("reason", "already_processed")}
        twin = _reviewed_event_item(session, anchor=raw, anchor_source=source)
        if twin is not None:
            if reassessing:
                documents = tuple(_document(item) for item, _ in
                                  _event_items(session, anchor=raw, anchor_source=source))
                persist_policy_reassessment(session, raw_id, EditorialOutcome(
                    status=PublicationStatus.SKIPPED, reason="event_already_reviewed",
                    ranking=RankingResult(should_process=False, relevance=0, category="city",
                                          confidence=1, rationale="Событие уже проверено"),
                    confidence=1), documents)
                return {"raw_item_id": raw_id, "status": "skipped",
                        "reason": "event_already_reviewed"}
            # The service refuses a second review of the same event; without a run this item
            # would fail the same way in every cycle and take a processing slot each time.
            session.add(EditorialRun(
                raw_item_id=raw_id, delivery_status="pending",
                outcome={"status": "skipped", "reason": "event_already_reviewed",
                         "duplicate_of_raw_item": twin.id, "ai_calls": 0},
                evidence={"raw_item_id": raw_id, "source_id": raw.source_id},
                editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
            ))
            session.commit()
            return {"raw_item_id": raw_id, "status": "skipped",
                    "reason": "event_already_reviewed"}
        members = _event_items(session, anchor=raw, anchor_source=source)
        documents = tuple(_document(item) for item, _ in members)
        snapshots = [_snapshot_member(item, policy) for item, policy in members]
        identity = dict(raw_item_id=raw.id, source_id=raw.source_id, article_id=raw.external_id)
        key = event_key(_candidate(raw))
        recent = recent_publications(session, source.city, now=datetime.now(UTC))
        (output / "rubric-plan.json").write_text(json.dumps(
            rubric_plan(profile, recent, now=datetime.now(UTC)),
            ensure_ascii=False, indent=2), encoding="utf-8")
    freshness = evidence_freshness(raw.title, raw.body, raw.published_at, datetime.now(UTC),
        precision=raw.published_at_precision, source_type=source.source_type,
        verified_at=raw.ingested_at, timezone_name=profile.timezone)
    venue_reason = (event_content_reason(raw.title, raw.body, now=datetime.now(UTC),
                                        timezone_name=profile.timezone)
                    if source.source_type == "official_venue_events" else None)
    if freshness.action != "keep" or venue_reason is not None:
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
        outcome = EditorialPipeline(provider, city_profile=profile).run(
            city=source.city, documents=documents,
            recent_publications=[{"id": str(row["id"]), "title": row["title"],
                                  "body": row["body"][:1200], "status": row["status"]}
                                 for row in recent[:20]], source_update=revision is not None,
            evergreen=source.source_type == GUIDE_SOURCE_TYPE,
            reader_report=is_reader_report_document(source.id))
    if (draft_text := outcome.draft) is not None:
        duplicate = find_repeated_publication(
            draft_text.title, draft_text.body,
            [(row["id"], row["title"], row["body"]) for row in recent])
        if duplicate is not None:
            outcome = outcome.model_copy(update={
                "status": PublicationStatus.SKIPPED,
                "reason": f"draft_repeats_recent_post:{duplicate}", "draft": None})
        elif revision is None and (same := find_same_story(
                draft_text.title, draft_text.body,
                [(row["id"], row["title"], row["body"], _aware(row["published_at"]))
                 for row in recent],
                now=datetime.now(UTC))) is not None:
            # Another source retold a story already in the feed (#333). Source updates
            # keep their own correction path; this only stops a second separate post.
            outcome = outcome.model_copy(update={
                "status": PublicationStatus.SKIPPED,
                "reason": f"draft_repeats_recent_story:{same}", "draft": None})
    needs_reviewed_package = (reassessing and outcome.status == PublicationStatus.HUMAN_REVIEW
                              and outcome.ranking.selection is not None)
    if outcome.draft is None or (outcome.status != PublicationStatus.READY
                                 and not needs_reviewed_package):
        with Session(engine) as session:
            run = (persist_policy_reassessment(session, raw_id, outcome, documents)
                   if reassessing else
                   EditorialService(session, _CachedOutcome(
                       outcome, documents, source.city)).prepare(raw_id))
            return {"raw_item_id": raw_id, "status": run.outcome["status"],
                    "reason": run.outcome["reason"],
                    "resolution": "deferred_policy" if run.outcome["status"] == "human_review"
                                  else "rejected"}
    draft = outcome.draft.model_copy(update={
        "title": presentation_title(outcome.draft.title),
    })
    assert outcome.ranking.selection is not None
    with Session(engine) as session:
        presentation = build_presentation(
            session, raw_id, documents, document_ids=[item.id for item, _ in members],
            profile=profile, rubric_id=outcome.ranking.selection.rubric_id,
            source_url=next((item.url for item, policy in members
                             if policy.ownership.strip().casefold() == "official"), raw.url))
    draft = present_draft(draft, presentation, category=outcome.ranking.category)
    if is_reader_report_document(source.id):
        with Session(engine) as session:
            submission = session.get(Submission, submission_id_for(raw) or 0)
            credit = reader_credit(submission.payload) if submission is not None else ""
        draft = draft.model_copy(update={"body": with_reader_credit(draft.body, credit)})
    from relocant_media.services.native_map import bind_native_map_draft

    presentation = bind_native_map_draft(presentation, draft)
    outcome = outcome.model_copy(update={"draft": draft})
    placeholders = [dict(reviewer_id=f"pending-{role}", role=role,
        source_id=identity["source_id"], article_id=identity["article_id"],
        context_sha256="0" * 64, draft_sha256="0" * 64, reviewed_at=datetime.now(UTC).isoformat(),
        verdict="hold", score=0, flags=["pending_review"], live_source_checked=False)
        for role in ("facts", "utility")]
    package = EditorialPackage.model_validate_json(json.dumps(dict(
        version=2, policy_version="subscription-editorial-v2", city=source.city, **identity,
        presentation=presentation.model_dump(mode="json"),
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
    # Исправления могут удалить неподтверждённую новизну: проверяем итог повторно.
    duplicate = find_repeated_publication(
        package.outcome.draft.title, package.outcome.draft.body,
        [(row["id"], row["title"], row["body"]) for row in recent])
    if duplicate is not None:
        rejected = EditorialOutcome.model_validate(package.outcome.model_dump()).model_copy(update={
            "status": PublicationStatus.SKIPPED,
            "reason": f"draft_repeats_recent_post:{duplicate}", "draft": None})
        with Session(engine) as session:
            run = (persist_policy_reassessment(session, raw_id, rejected, documents)
                   if reassessing else EditorialService(session, _CachedOutcome(
                       rejected, documents, source.city)).prepare(raw_id))
            return {"raw_item_id": raw_id, "status": run.outcome["status"],
                    "reason": run.outcome["reason"], "resolution": "rejected"}
    with Session(engine) as session:
        run = import_package(session, package, replace_empty_hold=reassessing,
                             replace_map_hold=reassessing)
        media_plan = build_media_preparation_packet(session, raw_id)
        (output / f"media-plan-{raw_id}.json").write_text(
            json.dumps(media_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        result = {"raw_item_id": raw_id, "status": run.outcome["status"],
                  "reason": run.outcome["reason"],
                  "resolution": "ready" if run.outcome["status"] == "ready" else "deferred_policy",
                  "review_scores": [r.score for r in package.reviews]}
    (output / f"package-{raw_id}.json").write_text(
        package.model_dump_json(indent=2), encoding="utf-8")
    return result


def policy_reassessment_ids(engine: Engine) -> tuple[list[int], list[int]]:
    corrections: list[int] = []
    policy_holds: list[int] = []
    with Session(engine) as session:
        reason = EditorialRun.outcome["reason"].as_string()
        correction_cutoff = datetime.now(UTC) - timedelta(days=8)
        rows = session.scalars(select(EditorialRun).join(RawItem).join(Source)
            .where(Source.city == "belgrade", (EditorialRun.post_id.is_(None)
                   | reason.in_(("event_place_requires_verification", "package_requires_review"))),
                   EditorialRun.delivery_status == "pending",
                   reason.in_(("source_auto_publish_disabled",
                               "event_place_requires_verification", "package_requires_review")) | (
                       reason.in_(("selection_ungrounded_evidence", "ranking_rejected"))
                       & (RawItem.published_at >= correction_cutoff)
                   ))
            .order_by(RawItem.published_at.desc())).all()
        for run in rows:
            if eligible_policy_reassessment(session, run.raw_item_id):
                target = (policy_holds if run.outcome["reason"] == "source_auto_publish_disabled"
                          else corrections)
                target.append(run.raw_item_id)
    return corrections, policy_holds


class ChannelPublisher:
    """Target cannot be chosen by a model, source, package or CLI argument."""
    def __init__(self, bot: TelegramBotAPI):
        self.bot = bot

    async def publish_followup(self, text: str, reply_to: int) -> int:
        formatted = editorial_message_html(text)
        if formatted is None or not valid_title_presentation(text.split("\n", 1)[0]):
            raise ValueError("follow-up requires a formatted reviewed post")
        html, source_url = formatted
        suffix = f'<a href="https://t.me/eduvbelgrad/{reply_to}">Первый пост</a>'
        if formatted_text_units(html + "\n\n" + suffix) <= TELEGRAM_TEXT_LIMIT:
            return await self.bot.send_notice_message(
                APPROVED_CHANNEL_ID, html + "\n\n" + suffix, source_url=source_url,
                reply_to=reply_to,
            )
        return await self.bot.send_rich_message(
            APPROVED_CHANNEL_ID, rich_editorial_html(html) + f"<p>{suffix}</p>",
            reply_to=reply_to,
        )

    async def publish(self, text: str) -> int:
        if not valid_title_presentation(text.split("\n", 1)[0]):
            raise ValueError("title presentation does not match channel mode")
        if formatted := editorial_message_html(text):
            html, source_url = formatted
            if formatted_text_units(html) <= TELEGRAM_TEXT_LIMIT:
                if callable(getattr(self.bot, "send_notice_message", None)):
                    return await self.bot.send_notice_message(
                        APPROVED_CHANNEL_ID, html, source_url=source_url,
                    )
            elif callable(getattr(self.bot, "send_rich_message", None)):
                return await self.bot.send_rich_message(
                    APPROVED_CHANNEL_ID, rich_editorial_html(html),
                )
        return await self.bot.send_message(APPROVED_CHANNEL_ID, text)


async def cleanup_news_pins(engine: Engine) -> dict[str, Any]:
    policy = json.loads((SOURCES.parent / "pin-policy.json").read_text(encoding="utf-8"))
    with Session(engine) as session:
        news_ids = set(session.scalars(select(Post.telegram_message_id).join(
            EditorialRun, EditorialRun.post_id == Post.id).where(
                Post.city == "belgrade", EditorialRun.delivery_status == "sent",
                Post.telegram_message_id.is_not(None))))
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        bot = TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client)
        result = await maintain_pins(
            bot, policy, news_message_ids={i for i in news_ids if i is not None},
        )
        try:
            # Только сверка: пост карты отправляет лишь ручной workflow.
            result["map_pin"] = await ensure_map_pin_on_top(bot, policy, apply=False)
        except (httpx.HTTPError, TelegramAPIError, ValueError) as error:
            result["map_pin"] = {"map_pin": "unknown", "error_type": type(error).__name__}
        return result


async def audience_observation() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        return await observe_audience(TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client))


def _attach_flight_review(session: Session, cycle_id: int, raw_id: int) -> None:
    cycle = session.get(SchedulerRun, cycle_id)
    if (cycle is None or cycle.task_key != "editorial-primary:belgrade"
            or cycle.task_type != "editorial_primary" or cycle.finished_at is None
            or cycle.status not in {"success", "partial"}):
        raise ValueError("Отсутствует завершённый цикл удержанной авиановости")
    ids = dict.fromkeys([raw_id, *cycle.details.get("flight_review_ids", [])])
    cycle.status = "partial"
    cycle.details = {**cycle.details, "status": "PRIMARY_PARTIAL", "window_complete": False,
                     "fallback_required": True, "flight_review_ids": list(ids)[:5]}


async def publish_pending(
    engine: Engine, *, enabled: bool, limit: int = 2,
    blocked: list[dict[str, Any]] | None = None,
    only_raw_ids: set[int] | None = None,
    scheduler_run_id: int | None = None,
) -> list[int]:
    if not enabled or only_raw_ids == set():
        return []
    from relocant_media.services.flight_review import FlightReviewRequired
    from relocant_media.services.material_lifecycle import (
        after_result,
        before_delivery_attempt,
        delivery_deadline,
        delivery_failure,
        hold_until_morning,
        request_flight_review,
    )
    with Session(engine) as session:
        outcome = EditorialRun.outcome
        package_fields = outcome["_package"]
        query = select(
            EditorialRun.raw_item_id,
            RawItem.published_at,
            outcome["ranking"]["category"].as_string().label("category"),
            package_fields["urgent"].as_json().label("urgent"),
            package_fields["presentation"]["event_place"].as_json().label("event_place"),
        ).join(RawItem, RawItem.id == EditorialRun.raw_item_id).where(
            EditorialRun.delivery_status == "pending",
        )
        if only_raw_ids is not None:
            query = query.where(EditorialRun.raw_item_id.in_(only_raw_ids))
        ids = delivery_order(
            (PendingDelivery(
                raw_id=row.raw_item_id, published_at=row.published_at,
                category=str(row.category or ""), urgent=row.urgent is True,
                event_place=row.event_place,
            ) for row in session.execute(query)),
            now=quiet_hours.utc_now(),
        )
        last_sent_at = session.scalar(select(func.max(Post.published_at)).join(
            EditorialRun, EditorialRun.post_id == Post.id).where(
                EditorialRun.delivery_status == "sent",
                Post.telegram_message_id.is_not(None)))
    if last_sent_at is not None and last_sent_at.tzinfo is None:
        last_sent_at = last_sent_at.replace(tzinfo=UTC)
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
                ranking = run.outcome.get("ranking")
                stored = run.outcome["_package"]
                category = (str(ranking.get("category", "")) if isinstance(ranking, dict)
                            else "")
                urgent = isinstance(stored, dict) and stored.get("urgent") is True
                now = quiet_hours.utc_now()
                release = morning_release(category=category, urgent=urgent, now=now)
                if release is not None:
                    # Not a failure: the morning cycle re-checks freshness and sends it.
                    hold_until_morning(session, raw_id, now=now, release_at=release,
                                       deadline=morning_deadline(release))
                    session.commit()
                    continue
                if pacing_hold(
                    category=category, urgent=urgent,
                    now=datetime.now(UTC), last_sent_at=last_sent_at,
                    deadline_at=delivery_deadline(session, raw_id),
                ):
                    # Not a failure: the next cycle sends it, before its deadline.
                    continue
                allowed = before_delivery_attempt(session, raw_id, now=datetime.now(UTC))
                session.commit()
                if not allowed:
                    continue

                def failed(error_type: str, failed_id: int = raw_id) -> None:
                    delivery_failure(session, failed_id, error_type, now=datetime.now(UTC))
                    session.commit()

                try:
                    package = EditorialPackage.model_validate_json(
                        json.dumps(run.outcome["_package"]))
                except ValueError:
                    failed("InvalidStoredPackage")
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id, "reason": "invalid_stored_package"})
                    continue
                if package.version != 2:
                    failed("UnsupportedPackageVersion")
                    continue
                assert package.event is not None
                item = session.get(RawItem, raw_id)
                assert item is not None
                fresh = freshness_decision(title=item.title, body=item.body,
                    published_at=item.published_at, now=datetime.now(UTC),
                    published_at_precision=item.published_at_precision,
                    timezone_name=load_city_profile(package.city).timezone)
                if fresh.action != "keep":
                    failed("SourceFreshnessExpired")
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
                    failed(type(error).__name__)
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id,
                                        "reason": "live_source_check_failed",
                                        "error_type": type(error).__name__})
                    continue
                try:
                    result = await deliver_reviewed(session, raw_id, publisher,
                        target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
                except FlightReviewRequired as error:
                    reason = request_flight_review(
                        session, raw_id, parent_raw_id=error.parent.raw_item_id,
                        parent_message_id=error.parent.message_id, now=datetime.now(UTC))
                    if reason == FLIGHT_REVIEW_REASON and scheduler_run_id is not None:
                        _attach_flight_review(session, scheduler_run_id, raw_id)
                    session.commit()
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id, "reason": reason,
                                        "parent_message_id": error.parent.message_id})
                    continue
                except PublishedTransportDuplicate:
                    continue
                except DeliveryBlockedError as error:
                    failed(type(error).__name__)
                    if blocked is not None:
                        blocked.append({"raw_item_id": raw_id, "reason": "delivery_guard_blocked",
                                        "error_type": type(error).__name__})
                    continue
                except Exception as error:
                    # После захвата sending транспорт сам сохраняет uncertain.
                    # Следующий запуск не имеет права повторять такую отправку.
                    failed(type(error).__name__)
                    raise
                assert result.message_id is not None
                after_result(session, raw_id, now=datetime.now(UTC))
                session.commit()
                sent.append(result.message_id)
                last_sent_at = datetime.now(UTC)
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
        models = role_models(os.environ)
        api_key = os.environ["GEMINI_API_KEY"]
        provider = PublicPreparationCache(
            ServerAI(RoleRoutedProvider(
                models, lambda model: IsolatedGeminiProvider(api_key, model=model)),
                audit_path=args.output / "ai-calls.jsonl", provider_name="gemini",
                model_id=models["default"]),
            Path(url.database).parent / "ai-cache",
            model_id=cache_model_id(models) + ":server-policy-v7-local-venue-style",
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
        feedback_status: dict[str, Any] = {}

        async def feedback() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=25, trust_env=False) as feedback_client:
                return await reply_to_submissions(engine,
                    TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], feedback_client),
                    enabled=os.environ.get("EDITORIAL_PRIVATE_FEEDBACK_ENABLED") == "true",
                    only_ids=(set(scope.get("submission_ids", []))
                              if args.submissions_only else None))

        def send_feedback() -> None:
            try:
                feedback_status.update(asyncio.run(feedback()))
            except Exception as feedback_error:
                feedback_status.update(status="failed", error_type=type(feedback_error).__name__)

        def ingest() -> dict[str, Any]:
            stats = asyncio.run(collect_sources(engine, output=args.output,
                                               submissions_only=args.submissions_only))
            scope.update(stats)
            # Reader questions can be sent as soon as source triage commits.
            # They should not wait behind model calls or public delivery.
            with measure_collection(stats.setdefault("collection_seconds", {}), "reader_feedback"):
                send_feedback()
            return stats
        def allowed_items() -> set[int]:
            return set(scope.get("submission_raw_ids", []))
        def deferred_items(session: Session) -> set[int]:
            return pending_gpt_ids(session, now=datetime.now(UTC))
        profile = load_city_profile("belgrade")
        local_places = load_queue_place_aliases("belgrade")

        def preselect(items: list[dict[str, Any]], limit: int
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
            with Session(engine) as session:
                recent = recent_publications(session, "belgrade", now=datetime.now(UTC))
            return preselect_candidates(
                items, provider, profile=profile, limit=limit, local_place_aliases=local_places,
                now=datetime.now(UTC), recent_publications=[
                    {"id": str(row["id"]), "title": row["title"], "body": row["body"]}
                    for row in recent[:20]],
            )
        gpt_fallback_enabled = (
            not args.submissions_only and os.environ.get("EDITORIAL_GPT_FALLBACK_ENABLED") == "true"
        )
        from relocant_media.services.material_lifecycle_runtime import MaterialLifecycleRuntime

        lifecycle = MaterialLifecycleRuntime(engine, "belgrade", provider)

        def lifecycle_due() -> tuple[list[int], set[int]]:
            # Резерв открывает цикл, который формирует запрос после сохранения состояния.
            sweep = lifecycle.reconcile(reissue_event_fallback=gpt_fallback_enabled)
            scope["lifecycle_fallback_ids"] = set(sweep.fallback_ids)
            return lifecycle_due_order(sweep.rejection_audit_ids, sweep.due_ids), set(
                sweep.excluded_ids)

        result = run_cycle(engine, city="belgrade", output=args.output, now=datetime.now(UTC),
            ingest=ingest,
            process=lambda raw_id: lifecycle.process(raw_id,
                lambda selected_id: prepare_reviewed(engine, selected_id,
                    lifecycle.provider_for(selected_id), args.output)),
            lifecycle_due=lifecycle_due,
            lifecycle_fallback_items=lambda: set(scope.get("lifecycle_fallback_ids", set())),
            limit=PROCESS_LIMIT,
            preselect=None if args.submissions_only else preselect,
            locality_aliases=profile.geography.city_aliases,
            local_place_aliases=local_places,
            refresh_until=True, newest_first=True, allow_partial_ingest=True,
            priority_items=private_priorities,
            recent_source_counts=source_load,
            reassessment_ids=(None if args.submissions_only
                              else lambda: policy_reassessment_ids(engine)),
            continue_on_error=True, submissions_only=args.submissions_only,
            allowed_items=allowed_items if args.submissions_only else None,
            deferred_items=deferred_items if gpt_fallback_enabled else None,
            # Порядок советующий: ошибка сравнения не отменяет полный разбор выбранных тем.
            defer_failed_batch=False,
            candidate_priorities=lambda: queue_priorities(
                source_priorities(json.loads(
                    (args.output / "venue-attention.json").read_text(encoding="utf-8")))
                if (args.output / "venue-attention.json").is_file() else {},
                media_sources()))
        result["private_intake"] = intake_status
        result["submissions_only"] = args.submissions_only
        if not feedback_status:
            send_feedback()
        result["private_feedback"] = feedback_status
        result["fallback_required"] = (bool(result.get("fallback_required"))
                                       or result["status"] == "PRIMARY_FAILED"
                                       or bool(result.get("processing_errors"))
                                       or bool(result.get("gpt_deferred_count")))
        result["sent_message_ids"] = []
        result["channel_transport"] = "disabled"
        if result["status"] in {"PRIMARY_OK", "PRIMARY_PARTIAL"}:
            enabled = os.environ.get("EDITORIAL_SEND_ENABLED") == "true"
            result["channel_transport"] = "bot-api" if enabled else "disabled"
            blocked: list[dict[str, Any]] = []
            result["sent_message_ids"] = asyncio.run(
                publish_pending(engine, enabled=enabled, blocked=blocked,
                    only_raw_ids=allowed_items() if args.submissions_only else None,
                    scheduler_run_id=None if args.submissions_only else result["run_id"]))
            result["channel_blocked"] = blocked
            if any(item["reason"] == FLIGHT_REVIEW_REASON for item in blocked):
                result.update(status="PRIMARY_PARTIAL", window_complete=False)
            result["fallback_required"] = result["fallback_required"] or bool(blocked)
            if enabled:
                result["pin_maintenance"] = asyncio.run(cleanup_news_pins(engine))
                audience = asyncio.run(audience_observation())
                (args.output / "audience-observation.json").write_text(
                    json.dumps(audience, ensure_ascii=False, indent=2), encoding="utf-8")
                result["audience_observation"] = audience
                with Session(engine) as kpi_session:
                    record_subscriber_observation(kpi_session, "belgrade", audience)
            with Session(engine) as audit_session:
                cycle_checks = audit_cycle(audit_session, result)
            (args.output / "cycle-audit.json").write_text(
                json.dumps(cycle_checks, ensure_ascii=False, indent=2), encoding="utf-8")
            result["cycle_audit_status"] = cycle_checks["status"]
    except Exception as error:
        frame = error.__traceback__
        while frame is not None and frame.tb_next is not None:
            frame = frame.tb_next
        result = {"status": "SERVER_FAILED", "error_type": type(error).__name__,
                  "error_site": (f"{Path(frame.tb_frame.f_code.co_filename).name}:"
                                 f"{frame.tb_lineno}" if frame is not None else "unknown"),
                  "fallback_required": True}
    finally:
        if engine is not None:
            engine.dispose()
    if args.output.exists():
        if os.environ.get("EDITORIAL_ACTIONS_CHECKPOINT") != "true":
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
        (args.output / "newsroom-summary.json").write_text(
            json.dumps(summarize(args.output), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] not in {"PRIMARY_OK", "PRIMARY_PARTIAL"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
