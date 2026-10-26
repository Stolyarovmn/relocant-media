"""Allowlisted, read-only public newsroom snapshot. Never export drafts or private intake."""
from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from relocant_media.collectors.telegram_topics import DEFAULT_CHANNELS_BY_CITY
from relocant_media.ops.public_observations import public_coverage
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.newsroom_control import build_newsroom_inbox
from relocant_media.sources.registry import load_source_registry
from relocant_media.storage.models import AIUsageEvent, EditorialRun, RawItem, Source

CITIES = {"belgrade": "Белград", "novi_sad": "Нови-Сад"}


def public_article_url(value: str, source_url: str) -> str | None:
    try:
        url, source = urlsplit(value), urlsplit(source_url)
        if (url.scheme == "https" and url.hostname and source.hostname
                and url.hostname.removeprefix("www.") == source.hostname.removeprefix("www.")
                and url.port in (None, 443) and not url.username and not url.password
                and not url.query and not url.fragment):
            return value
    except ValueError:
        pass
    return None


def iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat() if value else None


def catalog(root: Path, city: str) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for config in load_source_registry(root / "cities" / city / "sources"):
        rows[config.id] = {
            "id": config.id, "name": config.name, "url": str(config.url), "platform": "web",
            "configured_status": config.extraction.status or "unknown",
            "strategy": config.extraction.strategy, "runtime_enabled": None,
            "reason": config.verification.note, "high_risk": config.policy.high_risk,
            "auto_publish": config.policy.auto_publish,
        }
    discovery = root / ("docs/messenger-discovery.json" if city == "belgrade"
                        else f"cities/{city}/messenger-discovery.json")
    if discovery.is_file():
        for source in json.loads(discovery.read_text())["sources"]:
            if city not in source.get("scope", []):
                continue
            collection = source.get("collection", {})
            rows[source["id"]] = {
                "id": source["id"], "name": source["name"], "url": source["url"],
                "platform": source["platform"],
                "configured_status": collection.get("status", "unknown"),
                "strategy": collection.get("adapter"), "runtime_enabled": None,
                "reason": collection.get("reason", "discovery_only"),
                "auto_publish": False, "high_risk": None,
            }
    for channel in DEFAULT_CHANNELS_BY_CITY.get(city, ()):
        url = f"https://t.me/s/{channel}"
        if not any(row["url"] == url for row in rows.values()):
            rows[f"telegram_{channel}"] = {
                "id": f"telegram_{channel}", "name": channel, "url": url,
                "platform": "telegram", "configured_status": "runtime_public_preview",
                "strategy": "telegram_public_preview", "runtime_enabled": None,
                "reason": "discovery_only", "auto_publish": False, "high_risk": None,
            }
    return list(rows.values())


def city_snapshot(root: Path, city: str, session: Session | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": city, "name": CITIES[city], "sources": catalog(root, city),
        "runtime_status": "unavailable", "observed_at": None,
        "counts": None, "total_events": None, "items": None, "runs": None, "ai_usage": None,
        "attribution_note": "Нет связи discovery → модель: неизвестно, не ноль.",
    }
    if session is None:
        return result
    sources = {row.id: row for row in session.scalars(select(Source).where(Source.city == city))}
    # Reading a database does not establish that every configured city has a runtime.
    if not sources:
        return result
    result["runtime_source_count"] = len(sources)
    for row in result["sources"]:
        source = sources.get(row["id"])
        if source is not None:
            row.update(runtime_enabled=source.enabled, high_risk=source.high_risk,
                       auto_publish=source.auto_publish)
            if not source.enabled:
                row["reason"] = "source_disabled_in_database; original reason not recorded"
    inbox = build_newsroom_inbox(session, city=city, limit=500)
    public_items = []
    for item in inbox["items"]:
        raw = session.get(RawItem, item["anchor_raw_item_id"])
        assert raw is not None
        source = sources[raw.source_id]
        run = session.get(EditorialRun, raw.id)
        # A submission body/title can contain private data. Only official public-source
        # titles are eligible for this public projection; never copy run.outcome wholesale.
        public = {key: item[key] for key in (
            "event_key", "state", "reason_code", "raw_item_ids", "published_at",
            "review_status", "delivery_status", "post_id", "telegram_message_id",
            "editorial_rule_version", "source_revision_count", "last_error", "retry",
        )}
        if not re.fullmatch(r"[a-z][a-z0-9_:-]{0,159}", public["reason_code"]):
            public["reason_code"] = "editorial_decision_see_private_audit"
        public.update(title=raw.title[:300] if source.ownership == "official" else None,
                      source_id=source.id, discovered_by=None, editor_model=None, reviews=[],
                      published_at_precision=raw.published_at_precision)
        public["url"] = (public_article_url(raw.url, source.url)
                         if source.ownership == "official" else None)
        package = run.outcome.get("_package", {}) if run else {}
        if isinstance(package, dict):
            for review in package.get("reviews", []):
                if isinstance(review, dict):
                    public["reviews"].append({
                        key: review.get(key) for key in (
                            "role", "verdict", "score", "reviewed_at", "live_source_checked",
                        )
                    })
        public_items.append(public)
    result.update(runtime_status="available", observed_at=datetime.now(UTC).isoformat(),
                  counts=inbox["counts"], total_events=inbox["total_events"],
                  returned_events=inbox["returned_events"], items=public_items)
    result["runs"] = [
        {"id": run.id, "status": run.status, "started_at": iso(run.started_at),
         "finished_at": iso(run.finished_at), "error_type": run.error_type}
        for run in session.scalars(select(SchedulerRun).where(SchedulerRun.city == city)
                                  .order_by(SchedulerRun.id.desc()).limit(30))
    ]
    result["ai_usage"] = [
        {"provider": row.provider, "model": row.model, "task": row.task, "status": row.status,
         "occurred_at": iso(row.occurred_at), "input_tokens": row.input_tokens,
         "output_tokens": row.output_tokens}
        for row in session.scalars(select(AIUsageEvent).where(AIUsageEvent.city == city)
                                  .order_by(AIUsageEvent.id.desc()).limit(100))
    ]
    return result


def snapshot(root: Path, session: Session | None = None) -> dict[str, Any]:
    return {"schema_version": 1, "generated_at": datetime.now(UTC).isoformat(),
            "mode": "public_read_only", "cities": [
                city_snapshot(root, city, session) for city in CITIES],
            "limitations": ["Без текстов черновиков, предложки, контактов и секретов.",
                             "Jev shadow не принимает решения approve/reject.",
                             "Облачные ответы чата не попадают в БД автоматически."]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path,
                        help="existing canonical SQLite; never creates one")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-directory", type=Path)
    parser.add_argument("--observed-at", help="snapshot observation time; never refresh old data")
    args = parser.parse_args()
    if args.database:
        database = args.database.resolve()
        if not database.is_file():
            parser.error("existing canonical database required")
        engine = create_engine(f"sqlite:///file:{database.as_posix()}?mode=ro&uri=true")
        try:
            with Session(engine) as session:
                session.execute(text("PRAGMA query_only=ON"))
                payload = snapshot(args.root, session)
        finally:
            engine.dispose()
    else:
        payload = snapshot(args.root)
    if args.observed_at:
        observed = datetime.fromisoformat(args.observed_at)
        if observed.tzinfo is None or observed > datetime.now(UTC):
            parser.error("timezone-aware observation time not in the future required")
        for city in payload["cities"]:
            if city["runtime_status"] == "available":
                city["observed_at"] = observed.isoformat()
    if args.run_directory:
        report_path = args.run_directory / "server-report.json"
        manifest_path = args.run_directory / "manifest.json"
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
        manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
        # This server entrypoint currently operates on Belgrade only. Do not attribute
        # its errors, model observations or publication receipts to Novi Sad.
        cycle: dict[str, Any] = {
            "status": report.get("status"), "observed_at": datetime.now(UTC).isoformat(),
            "error_type": report.get("error_type"), "jev": None,
        }
        shadow = manifest.get("jev_shadow")
        if isinstance(shadow, dict):
            cycle["jev"] = {key: shadow.get(key) for key in (
                "status", "mode", "decision_effect", "evaluated", "model",
            )}
            cycle["jev"]["signals"] = [
                {key: row.get(key) for key in (
                    "raw_item_id", "local_relevance_probability", "practical_value_probability",
                )} for row in shadow.get("signals", []) if isinstance(row, dict)
            ]
        calls = args.run_directory / "ai-calls.jsonl"
        coverage = args.run_directory / "messenger-coverage.json"
        if coverage.is_file():
            cycle["messenger_coverage"] = public_coverage(
                json.loads(coverage.read_text()), payload["cities"][0]["sources"])
        cycle["ai_calls"] = []
        if calls.is_file():
            for line in calls.read_text().splitlines()[-200:]:
                if line.strip():
                    record = json.loads(line)
                    cycle["ai_calls"].append({key: record.get(key) for key in (
                        "provider", "model", "task", "status", "occurred_at", "duration_seconds",
                    )})
        payload["cities"][0]["last_cycle"] = cycle
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
