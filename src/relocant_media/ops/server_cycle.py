"""Bounded server newsroom; reviewed packages precede any enabled channel delivery."""
import argparse
import asyncio
import json
import os
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
from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.models import EditorialOutcome, EvidenceDocument, PublicationStatus
from relocant_media.ai.package import EditorialPackage
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.collectors.beograd_article import (
    BelgradeArticleCollector,
    parse_beograd_article,
)
from relocant_media.collectors.beograd_news import parse_beograd_news_list
from relocant_media.collectors.cukarica_news import parse_cukarica_article
from relocant_media.domain.dedup import event_key
from relocant_media.ingestion.persistence import capture_collected_batch
from relocant_media.ops.collect_cli import collect
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import (
    EditorialService,
    _auto_publish_policy_reason,
    _candidate,
    _document,
    _event_items,
    _snapshot_member,
)
from relocant_media.services.package_import import import_package
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import EditorialRun, RawItem, Source
from relocant_media.storage.sqlite_schema import verify_sqlite_schema

SOURCES = Path("cities/belgrade/sources")
PREFIX = "[тестовый запуск]"


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


async def collect_sources(engine: Engine) -> dict[str, int]:
    result = await collect("belgrade", SOURCES, ("city_beograd_news", "cukarica_news"))
    source = load_source_file(SOURCES / "city_beograd_news.yaml")
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        page = await client.get(
            "https://www.beograd.rs/lat/servisne-informacije/saobracaj-i-prevoz")
        page.raise_for_status()
        links = parse_beograd_news_list(page.text, source)
        if not links or len(links) > 100:
            raise ValueError("unexpected transport list")
        # Previously collected records remain in the durable queue. Discovery
        # from this additional section is bounded to the recent seven days.
        links = [item for item in links if item.published_at is not None
                 and item.published_at >= datetime.now(UTC) - timedelta(days=7)]
        collector = BelgradeArticleCollector(source, client)
        articles = [await collector.collect_url(item.url) for item in links[:30]]
    with Session(engine) as session:
        written, changes = capture_collected_batch(session, articles)
        session.commit()
    result["raw_items_written"] += written
    result["review_required"] += changes
    return result


def prepare_reviewed(
    engine: Engine, raw_id: int, provider: StructuredAIProvider, output: Path,
) -> dict[str, Any]:
    with Session(engine) as session:
        raw = session.get(RawItem, raw_id)
        assert raw is not None
        source = session.get(Source, raw.source_id)
        assert source is not None
        members = _event_items(session, anchor=raw, anchor_source=source)
        documents = tuple(_document(item) for item, _ in members)
        snapshots = [_snapshot_member(item, policy) for item, policy in members]
        policy_reason = _auto_publish_policy_reason(members, anchor_source=source)
        identity = dict(raw_item_id=raw.id, source_id=raw.source_id, article_id=raw.external_id)
        key = event_key(_candidate(raw))
    if policy_reason:
        from relocant_media.ai.models import RankingResult
        outcome = EditorialOutcome(status=PublicationStatus.HUMAN_REVIEW, reason=policy_reason,
            ranking=RankingResult(should_process=False, relevance=0, category="city",
                                  confidence=0, rationale=policy_reason), confidence=0)
    else:
        outcome = EditorialPipeline(provider).run(city="belgrade", documents=documents)
    if outcome.status != PublicationStatus.READY or outcome.draft is None:
        with Session(engine) as session:
            run = EditorialService(session, _CachedOutcome(outcome, documents)).prepare(raw_id)
            return {"raw_item_id": raw_id, "status": run.outcome["status"]}
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
        package = reviewed_package(package, provider, lambda: fetch_documents(package),
                                   audit=audit.append)
    finally:
        (output / f"review-audit-{raw_id}.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    with Session(engine) as session:
        run = import_package(session, package)
        result = {"raw_item_id": raw_id, "status": run.outcome["status"],
                  "review_scores": [r.score for r in package.reviews]}
    (output / f"package-{raw_id}.json").write_text(
        package.model_dump_json(indent=2), encoding="utf-8")
    return result


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
                              http_options={"timeout": 120000})
        provider = GeminiStructuredProvider(os.environ["GEMINI_API_KEY"],
            model=os.environ["GEMINI_MODEL"], client=client)
        result = run_cycle(engine, city="belgrade", output=args.output, now=datetime.now(UTC),
            ingest=lambda: asyncio.run(collect_sources(engine)),
            process=lambda raw_id: prepare_reviewed(engine, raw_id, provider, args.output), limit=2)
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
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] not in {"PRIMARY_OK", "PRIMARY_PARTIAL"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
