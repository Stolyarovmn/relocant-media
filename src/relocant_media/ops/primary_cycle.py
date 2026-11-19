"""Bounded primary preparation with durable PostgreSQL windows; no real publisher."""
import argparse
import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.ai.jev import jev_enabled, rank_shadow
from relocant_media.domain.freshness import GUIDE_SOURCE_TYPE
from relocant_media.ops.candidates_cli import aware_datetime, select_candidates
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.editorial import source_replacement_target
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
    reassessment_ids: Callable[[], tuple[list[int], list[int]]] | None = None,
    continue_on_error: bool = False,
    candidate_priorities: Callable[[], dict[str, int]] | None = None,
    priority_items: Callable[[], set[int]] | None = None,
    recent_source_counts: Callable[[], dict[str, int]] | None = None,
    allowed_items: Callable[[], set[int]] | None = None,
    deferred_items: Callable[[Session], set[int]] | None = None,
    submissions_only: bool = False,
    locality_aliases: tuple[str, ...] = (),
    local_place_aliases: tuple[str, ...] = (),
    preselect: Callable[[list[dict[str, Any]], int],
                        tuple[list[dict[str, Any]], dict[str, Any]]] | None = None,
    defer_failed_batch: bool = False,
    lifecycle_due: Callable[[], tuple[list[int], set[int]]] | None = None,
    lifecycle_fallback_items: Callable[[], set[int]] | None = None,
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
            corrections, policy_holds = reassessment_ids() if reassessment_ids else ([], [])
            priority_ids = priority_items() if priority_items else set()
            due_ids, lifecycle_excluded = lifecycle_due() if lifecycle_due else ([], set())
            allowed = allowed_items() if allowed_items else None
            retry_ids = [raw_id for raw_id in dict.fromkeys(due_ids)
                         if allowed is None or raw_id in allowed][:min(2, limit)]
            normal_limit = limit - len(retry_ids)
            with Session(engine) as session:
                deferred = deferred_items(session) if deferred_items else set()
                # Просроченные/закрытые материалы и ещё не наступившие повторы
                # исключаются независимо от наличия резервного обработчика.
                deferred = (deferred - set(due_ids)) | lifecycle_excluded
                manifest = select_candidates(session, city=city,
                    since=aware_datetime(window["since"]),
                    until=aware_datetime(window["until"]),
                    limit=30 if preselect else limit, newest_first=newest_first,
                    priority_raw_ids=priority_ids,
                    allowed_raw_ids=allowed_items() if allowed_items else None,
                    excluded_raw_ids=deferred | set(retry_ids),
                    recent_source_counts=recent_source_counts() if recent_source_counts else None,
                    locality_aliases=locality_aliases,
                    local_place_aliases=local_place_aliases,
                    source_priorities=candidate_priorities() if candidate_priorities else None)
            gpt_waiting = False
            gpt_candidate_ids: list[int] = []
            if preselect is not None:
                candidates = manifest["candidates"]
                protected = [candidate for candidate in candidates if (
                    candidate["raw_item_id"] in priority_ids
                    or candidate["source"]["type"] == GUIDE_SOURCE_TYPE
                    or candidate["source"]["ownership"] == "official" and re.search(
                        r"obustav|izmen.*saobra|preusmer|затвор|обустав|измен.*саобра",
                        candidate["evidence"]["title"], re.IGNORECASE,
                    )
                )][:normal_limit]
                protected_ids = {item["raw_item_id"] for item in protected}
                pool = [item for item in candidates if item["raw_item_id"] not in protected_ids]
                batch_slots = normal_limit - len(protected)
                selected, audit = preselect(pool, batch_slots)
                if (len(selected) > batch_slots
                        or any(type(item.get("raw_item_id")) is not int for item in selected)
                        or len({item["raw_item_id"] for item in selected}) != len(selected)
                        or any(not any(item is original for original in pool)
                               for item in selected)):
                    raise ValueError("preselection returned invalid candidate objects")
                if defer_failed_batch and audit.get("status") == "fallback" and batch_slots:
                    # Только исходные кандидаты из того же окна, которые прошли
                    # проверку свежести перед запросом Gemini.
                    order = [row["raw_item_id"] for row in selected] + audit["deferred_ids"]
                    pool_ids = {row["raw_item_id"] for row in pool}
                    if (any(type(raw_id) is not int or raw_id not in pool_ids for raw_id in order)
                            or len(set(order)) != len(order)):
                        raise ValueError("preselection returned invalid fallback IDs")
                    public_ids = {row["raw_item_id"] for row in pool
                                  if row["source"]["ownership"] in {"official", "media"}
                                  and row["raw_item_id"] not in priority_ids}
                    gpt_candidate_ids = [raw_id for raw_id in order if raw_id in public_ids]
                    gpt_waiting = bool(gpt_candidate_ids)
                    if gpt_waiting:
                        audit = {**audit, "status": "gpt_waiting",
                                 "gpt_candidate_ids": gpt_candidate_ids,
                                 "gpt_slots": batch_slots,
                                 "selected_ids": [], "deferred_ids": gpt_candidate_ids}
                        selected = []
                        heartbeat["fallback_required"] = True
                manifest["batch_preselection"] = {**audit,
                    "protected_ids": [item["raw_item_id"] for item in protected]}
                manifest["candidates"] = [*protected, *selected]
                manifest["candidate_count"] = len(manifest["candidates"])
                heartbeat["batch_preselection"] = manifest["batch_preselection"]
            if reassessment_ids is not None and not gpt_waiting:
                selected_ids = {candidate["raw_item_id"] for candidate in manifest["candidates"]}
                corrections = [i for i in corrections
                               if i not in selected_ids and i not in deferred]
                policy_holds = [i for i in policy_holds
                                if i not in selected_ids and i not in deferred]
                # Repair one previously stalled decision even if the fresh queue is full.
                if corrections:
                    manifest["candidates"].insert(0, {
                        "raw_item_id": corrections[0], "reassessment": True,
                    })
                    del manifest["candidates"][normal_limit:]
                slots = normal_limit - len(manifest["candidates"])
                manifest["candidates"].extend({"raw_item_id": i, "reassessment": True}
                    for i in (corrections[1:] + policy_holds)[:slots])
                manifest["candidate_count"] = len(manifest["candidates"])
                manifest["reassessment_count"] = len(corrections) + len(policy_holds)
            if due_ids:
                # Два места для наступивших повторов и проверки отказов. Новый
                # поток сохраняет штатный отбор, но не вытесняет старые ошибки.
                retry_set = set(retry_ids)
                manifest["candidates"] = [
                    *({"raw_item_id": raw_id, "lifecycle_retry": True} for raw_id in retry_ids),
                    *(item for item in manifest["candidates"]
                      if item["raw_item_id"] not in retry_set),
                ][:limit]
                manifest["candidate_count"] = len(manifest["candidates"])
                heartbeat["lifecycle_due_count"] = len(due_ids)
            if preselect is not None:
                heartbeat["batch_preselection"]["full_review_ids"] = [
                    candidate["raw_item_id"] for candidate in manifest["candidates"]]
            # Jev is a non-authoritative, optional observer; it never edits the queue.
            if jev_enabled():
                try:
                    manifest["jev_shadow"] = rank_shadow(manifest["candidates"], city=city)
                except Exception as error:
                    # No model errors or secrets may block the editorial checkpoint.
                    manifest["jev_shadow"] = {
                        "mode": "shadow_only",
                        "decision_effect": "none",
                        "status": "unavailable",
                        "error_type": type(error).__name__,
                    }
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
                    frame = error.__traceback__
                    while frame is not None and frame.tb_next is not None:
                        frame = frame.tb_next
                    with Session(engine) as session:
                        failed_id = source_replacement_target(session, raw_id) or raw_id
                    processing_errors.append({"raw_item_id": failed_id,
                        "error_type": type(error).__name__,
                        "error_site": (f"{Path(frame.tb_frame.f_code.co_filename).name}:"
                                       f"{frame.tb_lineno}" if frame is not None else "unknown")})
                    continue
                (output / f"editorial-{raw_id}.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                if result.get("replaced_unavailable_raw_item_id") == raw_id:
                    with Session(engine) as session:
                        raw_id = source_replacement_target(session, raw_id) or raw_id
                if raw_id not in processed:
                    processed.append(raw_id)
            with Session(engine) as session:
                # Повторный sweep учитывает только что завершённые попытки.
                if lifecycle_due:
                    remaining_due, lifecycle_excluded = lifecycle_due()
                else:
                    remaining_due = []
                remaining = select_candidates(session, city=city,
                    since=aware_datetime(window["since"]),
                    until=aware_datetime(window["until"]), limit=1,
                    allowed_raw_ids=allowed_items() if allowed_items else None,
                    excluded_raw_ids=lifecycle_excluded)
                revision_count = session.scalar(select(func.count()).select_from(
                    SourceRevision).join(RawItem).join(Source).where(Source.city == city))
            additional_remaining = (len(remaining_due) if lifecycle_due else
                sum(map(len, reassessment_ids())) if reassessment_ids else 0)
            fallback_ids = sorted(lifecycle_fallback_items())[:limit] if (
                lifecycle_fallback_items) else []
            if fallback_ids:
                heartbeat["fallback_required"] = True
                heartbeat["lifecycle_fallback_ids"] = fallback_ids
            complete = (remaining["eligible_count"] + additional_remaining == 0
                        and not processing_errors and not gpt_waiting and not fallback_ids)
            heartbeat.update(status="PRIMARY_OK" if complete else "PRIMARY_PARTIAL",
                window_complete=complete, processed_raw_item_ids=processed,
                processing_errors=processing_errors,
                gpt_deferred_count=len(deferred.union(gpt_candidate_ids)),
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
