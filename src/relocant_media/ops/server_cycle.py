"""Bounded server newsroom; reviewed packages precede any enabled channel delivery."""
import argparse
import asyncio
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from google import genai
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from relocant_media.ai.gemini import GeminiStructuredProvider
from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.package import EditorialPackage
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.ai.review_repair import review_with_repairs
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.collectors.beograd_article import (
    BelgradeArticleCollector,
    match_beograd_article_url,
    parse_beograd_article,
)
from relocant_media.collectors.beograd_news import parse_beograd_news_list
from relocant_media.collectors.cukarica_news import (
    ARTICLE_PATH,
    CukaricaNewsCollector,
    parse_cukarica_article,
    parse_cukarica_news_list,
)
from relocant_media.collectors.telegram_topics import DEFAULT_CHANNELS, collect_topics
from relocant_media.domain.dedup import event_key
from relocant_media.domain.freshness import freshness_decision
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops.newsroom_status import summarize
from relocant_media.ops.newsroom_triage import triage_queue
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import (
    EditorialService,
    _candidate,
    _document,
    _event_items,
    _snapshot_member,
)
from relocant_media.services.package_import import import_package
from relocant_media.services.policy_reassessment import (
    eligible_empty_policy_hold,
    persist_policy_reassessment,
)
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import EditorialRun, RawItem, Source
from relocant_media.storage.sqlite_schema import verify_sqlite_schema

SOURCES = Path("cities/belgrade/sources")
PREFIX = "[тестовый запуск]"


class ServerAI:
    """Keep server roles Russian and bind relevance decisions to the actual date."""
    def __init__(self, provider: StructuredAIProvider, *, audit_path: Path | None = None):
        self.provider = provider
        self.audit_path = audit_path

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        policy = ("Все пояснения, причины, оценки и тексты пиши по-русски. "
                  "Сербские топонимы и точные цитаты сохраняй. "
                  f"Текущий момент UTC: {datetime.now(UTC).isoformat()}. "
                  "Отличай дату статьи от срока события: истёкшие объявления не выбирай "
                  "для новой публикации и не выдавай планы за выполненные факты. ")
        started = time.monotonic()
        record: dict[str, Any] = {"task": task}
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


def fetch_documents(package: EditorialPackage) -> tuple[EvidenceDocument, ...]:
    assert package.event is not None
    documents = []
    with httpx.Client(timeout=25, trust_env=False) as client:
        for document in package.event.documents:
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


async def collect_sources(engine: Engine, *, output: Path | None = None) -> dict[str, int]:
    now = datetime.now(UTC)
    result = {"sources_synced": 2, "sources_skipped": 0, "raw_items_written": 0,
              "failures": 0, "review_required": 0, "discarded_before_article_fetch": 0,
              "discarded_before_persistence": 0, "channel_failures": 0, "channel_leads": 0}
    sources = {key: load_source_file(SOURCES / f"{key}.yaml")
               for key in ("city_beograd_news", "cukarica_news")}
    with Session(engine) as session:
        for source in sources.values():
            sync_source(session, "belgrade", source)
        session.commit()
    topics: list[dict[str, Any]] = []
    channel_errors: list[dict[str, str]] = []
    articles = []
    fetched: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        collectors: dict[str, BelgradeArticleCollector | CukaricaNewsCollector] = {
            "city_beograd_news": BelgradeArticleCollector(sources["city_beograd_news"], client),
            "cukarica_news": CukaricaNewsCollector(sources["cukarica_news"], client),
        }

        async def full_article(source_id: str, url: str) -> str:
            if url in fetched:
                return fetched[url]
            item = await collectors[source_id].collect_url(url)
            fresh = freshness_decision(item.title, item.body, item.published_at, now)
            if fresh.action != "keep":
                result["discarded_before_persistence"] += 1
                fetched[url] = fresh.reason
                return fresh.reason
            articles.append(item)
            fetched[url] = "primary_source_collected"
            return "primary_source_collected"

        pages = [("city_beograd_news", str(sources["city_beograd_news"].url)),
                 ("cukarica_news", str(sources["cukarica_news"].url)),
                 ("city_beograd_news",
                  "https://www.beograd.rs/lat/servisne-informacije/saobracaj-i-prevoz")]
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
        for channel in DEFAULT_CHANNELS:
            try:
                leads = await collect_topics(channel, client, now=now)
                for lead in leads[:20]:
                    result["channel_leads"] += 1
                    resolved = []
                    primary_urls = []
                    for url in lead.outbound_urls[:10]:
                        # Telegram text never becomes evidence. Fetch only independently
                        # parsed article URLs from the explicitly approved sources.
                        if match_beograd_article_url(url):
                            resolved.append(await full_article("city_beograd_news", url))
                            primary_urls.append(url)
                        else:
                            parts = urlsplit(url)
                            if (parts.scheme == "https"
                                    and parts.hostname in {"cukarica.rs", "www.cukarica.rs"}
                                    and parts.username is None and parts.password is None
                                    and parts.port in {None, 443}
                                    and ARTICLE_PATH.fullmatch(parts.path)):
                                resolved.append(await full_article("cukarica_news", url))
                                primary_urls.append(url)
                    # A matching official title is a discovery pointer only. The
                    # pipeline still has to check every claim against the full article.
                    words = set(re.findall(r"[^\W\d_]{5,}", lead.text.casefold()))
                    for article in articles:
                        title_words = set(re.findall(r"[^\W\d_]{5,}", article.title.casefold()))
                        if len(words & title_words) >= 3 and article.url not in primary_urls:
                            primary_urls.append(article.url)
                            resolved.append("primary_source_collected")
                    topics.append({"url": lead.url, "channel": channel,
                        "published_at": lead.published_at.isoformat(),
                        "status": "primary_source_resolved" if any(
                            r in {"primary_source_collected", "already_collected"}
                            for r in resolved) else "deferred_missing_primary_source",
                        "primary_results": resolved, "primary_urls": primary_urls, "ai_calls": 0})
            except Exception as error:
                result["channel_failures"] += 1
                channel_errors.append({"channel": channel, "error_type": type(error).__name__})
    with Session(engine) as session:
        written, changes = capture_collected_batch(session, articles)
        session.commit()
    result["raw_items_written"] = written
    result["review_required"] = changes
    triage = triage_queue(engine, now=now)
    result["discarded_queue"] = triage["discarded"]
    if output is not None:
        (output / "triage.json").write_text(json.dumps(triage, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        (output / "channel-topics.json").write_text(json.dumps({"topics": topics,
            "failures": channel_errors, "publication_evidence": False},
            ensure_ascii=False, indent=2), encoding="utf-8")
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
    if freshness.action != "keep":
        from relocant_media.ai.models import RankingResult
        outcome = EditorialOutcome(
            status=(PublicationStatus.SKIPPED if freshness.action == "discard"
                    else PublicationStatus.HUMAN_REVIEW), reason=freshness.reason,
            ranking=RankingResult(should_process=False, relevance=0, category="city",
                confidence=0, rationale=freshness.reason), confidence=0)
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
        "title": PREFIX + " " + outcome.draft.title.removeprefix(PREFIX).strip(),
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
        if not text.startswith(PREFIX):
            raise ValueError("required title prefix missing")
        return await self.bot.send_message(APPROVED_CHANNEL_ID, text)


async def publish_pending(engine: Engine, *, enabled: bool, limit: int = 2) -> list[int]:
    if not enabled:
        return []
    with Session(engine) as session:
        ids = list(session.scalars(select(EditorialRun.raw_item_id).where(
            EditorialRun.delivery_status == "pending",
        ).order_by(EditorialRun.raw_item_id)))
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
                package = EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
                if package.version != 2:
                    continue
                assert package.event is not None
                item = session.get(RawItem, raw_id)
                assert item is not None
                fresh = freshness_decision(title=item.title, body=item.body,
                    published_at=item.published_at, now=datetime.now(UTC))
                if fresh.action != "keep":
                    continue
                # Independent reviews and one final exact live-source comparison
                # must all remain valid before the durable send claim.
                live = fetch_documents(package)
                if any(a.model_dump() != b.model_dump(include=set(EvidenceDocument.model_fields))
                       for a, b in zip(live, package.event.documents, strict=True)):
                    raise ValueError("live source changed before delivery")
                result = await deliver_reviewed(session, raw_id, publisher,
                    target_channel_id=APPROVED_CHANNEL_ID, enabled=True)
                assert result.message_id is not None
                sent.append(result.message_id)
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="Серверная редакция с двумя ревью и квитанциями")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    engine = None
    try:
        url = make_url(os.environ["DATABASE_URL"])
        if (url.get_backend_name() != "sqlite" or not url.database
                or not Path(url.database).is_file() or url.query):
            raise ValueError("existing local SQLite required")
        engine = create_engine(url)
        verify_sqlite_schema(engine)
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                              http_options={"timeout": 45000, "retry_options": {"attempts": 1}})
        provider = ServerAI(GeminiStructuredProvider(os.environ["GEMINI_API_KEY"],
            model=os.environ["GEMINI_MODEL"], client=client),
            audit_path=args.output / "ai-calls.jsonl")
        result = run_cycle(engine, city="belgrade", output=args.output, now=datetime.now(UTC),
            ingest=lambda: asyncio.run(collect_sources(engine, output=args.output)),
            process=lambda raw_id: prepare_reviewed(engine, raw_id, provider, args.output), limit=2,
            refresh_until=True, newest_first=True, allow_partial_ingest=True,
            reassessment_ids=lambda: policy_reassessment_ids(engine))
        result["fallback_required"] = result["status"] == "PRIMARY_FAILED"
        result["sent_message_ids"] = []
        result["channel_transport"] = "disabled"
        if result["status"] in {"PRIMARY_OK", "PRIMARY_PARTIAL"}:
            enabled = os.environ.get("EDITORIAL_SEND_ENABLED") == "true"
            result["channel_transport"] = "bot-api" if enabled else "disabled"
            result["sent_message_ids"] = asyncio.run(publish_pending(engine, enabled=enabled))
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
