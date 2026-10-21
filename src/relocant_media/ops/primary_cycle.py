"""Bounded primary preparation with durable PostgreSQL windows; no real publisher."""
import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.ops.candidates_cli import aware_datetime, select_candidates
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.storage.models import EditorialRun, RawItem, Source, SourceRevision
from relocant_media.storage.primary_lock import primary_lock
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def begin_window(
    session: Session, *, city: str, now: datetime, refresh_until: bool = False,
    submissions_only: bool = False,
) -> SchedulerRun:
    """Caller must hold the primary session advisory lock across commits."""
    now = aware_datetime(now.isoformat())
    key = f"editorial-primary:{city}"
    if submissions_only:
        key += ":submissions"
    previous = session.scalar(select(SchedulerRun).where(
        SchedulerRun.task_key == key,
    ).order_by(SchedulerRun.id.desc()).limit(1))
    since, until = now - timedelta(hours=24), now
    if previous is not None:
        window = previous.details["window"]  # damaged checkpoints fail closed
        old_since = aware_datetime(window["since"])
        old_until = aware_datetime(window["until"])
        if previous.status == "success":
            since = old_until - timedelta(minutes=10)
        else:
            since, until = old_since, now if refresh_until else old_until
    if previous is None or previous.status == "success":
        # Late discoveries must not disappear behind a successful watermark.
        oldest = session.scalar(select(func.min(RawItem.published_at)).join(Source).where(
            Source.city == city, Source.enabled.is_(True),
            ~select(EditorialRun.raw_item_id).where(
                EditorialRun.raw_item_id == RawItem.id,
            ).exists(),
        ))
        if oldest is not None:
            since = min(since, oldest)
    if since >= until:
        raise ValueError("invalid primary window")
    attempt = session.scalar(select(func.max(SchedulerRun.attempt)).where(
        SchedulerRun.task_key == key, SchedulerRun.scheduled_for == until,
    )) or 0
    run = SchedulerRun(task_key=key, task_type="editorial_primary", city=city,
                       scheduled_for=until, attempt=attempt + 1, status="running",
                       started_at=now, details={"window": {
                           "since": since.isoformat(), "until": until.isoformat(),
                       }})
    session.add(run)
    session.commit()
    return run


def run_cycle(
    engine: Engine, *, city: str, output: Path, now: datetime,
    ingest: Callable[[], dict[str, Any]],
    process: Callable[[int], dict[str, Any]], limit: int = 5,
    refresh_until: bool = False, newest_first: bool = False,
    allow_partial_ingest: bool = False,
    reassessment_ids: Callable[[], list[int]] | None = None,
    continue_on_error: bool = False,
    candidate_priorities: Callable[[], dict[str, int]] | None = None,
    priority_items: Callable[[], set[int]] | None = None,
    recent_source_counts: Callable[[], dict[str, int]] | None = None,
    allowed_items: Callable[[], set[int]] | None = None,
    submissions_only: bool = False,
) -> dict[str, Any]:
    if (engine.dialect.name not in {"postgresql", "sqlite"}
            or not city.strip() or not 1 <= limit <= 5):
        raise ValueError("supported database, city and limit 1..5 required")
    if output.exists():
        raise ValueError("new output directory required")
    verify_sqlite_schema(engine)
    output.mkdir(parents=True, exist_ok=False)
    # A session-level lock survives commits performed by EditorialService. The
    # connection remains checked out until release; a crashed worker releases it.
    lock_key = int.from_bytes(hashlib.sha256(f"primary-cycle:{city}".encode()).digest()[:8],
                              "big", signed=True)
    with primary_lock(engine, lock_key) as acquired:
        if not acquired:
            return {"status": "PRIMARY_BUSY", "city": city, "window_complete": False}
        with Session(engine) as session:
            run = begin_window(session, city=city, now=now, refresh_until=refresh_until,
                               submissions_only=submissions_only)
            run_id, window = run.id, run.details["window"]
        heartbeat: dict[str, Any] = {
            "status": "PRIMARY_FAILED", "city": city, "run_id": run_id,
            "window": window, "window_complete": False, "telegram_transport": "fake",
        }
        try:
            collection = ingest()
            (output / "ingest.json").write_text(json.dumps(collection), encoding="utf-8")
            heartbeat["collection_failures"] = collection["failures"]
            if collection["failures"] and not allow_partial_ingest:
                raise ValueError("source collection incomplete")
            with Session(engine) as session:
                manifest = select_candidates(session, city=city,
                    since=aware_datetime(window["since"]),
                    until=aware_datetime(window["until"]), limit=limit, newest_first=newest_first,
                    priority_raw_ids=priority_items() if priority_items else None,
                    allowed_raw_ids=allowed_items() if allowed_items else None,
                    recent_source_counts=recent_source_counts() if recent_source_counts else None,
                    source_priorities=candidate_priorities() if candidate_priorities else None)
            if reassessment_ids is not None:
                selected = {candidate["raw_item_id"] for candidate in manifest["candidates"]}
                extra = [i for i in reassessment_ids() if i not in selected]
                manifest["candidates"].extend({"raw_item_id": i, "reassessment": True}
                    for i in extra[:max(0, limit - len(selected))])
                manifest["candidate_count"] = len(manifest["candidates"])
                manifest["reassessment_count"] = len(extra)
            (output / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            processed = []
            processing_errors = []
            for candidate in manifest["candidates"]:
                raw_id = candidate["raw_item_id"]
                try:
                    result = process(raw_id)
                except Exception as error:
                    if not continue_on_error:
                        raise
                    processing_errors.append({"raw_item_id": raw_id,
                                              "error_type": type(error).__name__})
                    continue
                (output / f"editorial-{raw_id}.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                processed.append(raw_id)
            with Session(engine) as session:
                remaining = select_candidates(session, city=city,
                    since=aware_datetime(window["since"]),
                    until=aware_datetime(window["until"]), limit=1,
                    allowed_raw_ids=allowed_items() if allowed_items else None)
                revision_count = session.scalar(select(func.count()).select_from(
                    SourceRevision).join(RawItem).join(Source).where(Source.city == city))
            additional_remaining = len(reassessment_ids()) if reassessment_ids else 0
            complete = (remaining["eligible_count"] + additional_remaining == 0
                        and not processing_errors)
            heartbeat.update(status="PRIMARY_OK" if complete else "PRIMARY_PARTIAL",
                window_complete=complete, processed_raw_item_ids=processed,
                processing_errors=processing_errors,
                remaining_count=remaining["eligible_count"] + additional_remaining,
                missing_published_at_count=remaining["missing_published_at_count"],
                review_required=collection.get("review_required", 0),
                revision_snapshots=revision_count)
        except Exception as error:
            # Do not persist exception messages: API errors can contain secrets.
            heartbeat["error_type"] = type(error).__name__
        with Session(engine) as session:
            stored = session.get(SchedulerRun, run_id)
            assert stored is not None
            stored.status = ("success" if heartbeat["window_complete"] else
                             "partial" if heartbeat["status"] == "PRIMARY_PARTIAL"
                             else "failed")
            stored.finished_at = datetime.now(UTC)
            stored.details = heartbeat
            session.commit()
        (output / "heartbeat.json").write_text(
            json.dumps(heartbeat, ensure_ascii=False, indent=2), encoding="utf-8")
        return heartbeat


def main() -> None:
    parser = argparse.ArgumentParser(description="Подготовить ограниченную пачку без потери хвоста")
    parser.add_argument("--city", default="belgrade")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if os.environ.get("APP_ENV", "").lower() != "staging" or not os.getenv("DATABASE_URL"):
        parser.exit(1, "Нужны явные APP_ENV=staging и DATABASE_URL.\n")
    if not os.getenv("GEMINI_API_KEY"):
        parser.exit(1, "Не настроен GEMINI_API_KEY.\n")
    from relocant_media.ops.collect_cli import collect
    from relocant_media.ops.editorial_cli import _run

    if args.city != "belgrade":
        parser.exit(1, "Пока настроен только Белград.\n")
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        result = run_cycle(engine, city=args.city, output=args.output, now=datetime.now(UTC),
            ingest=lambda: asyncio.run(collect(args.city, Path("cities/belgrade/sources"),
                                              ("city_beograd_news", "cukarica_news"))),
            process=lambda raw_id: asyncio.run(_run(raw_id, False, "fake")))
    except Exception:
        parser.exit(1, "Primary не завершён: проверьте БД, схему и журнал запуска.\n")
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False))
    if result["status"] != "PRIMARY_OK":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
