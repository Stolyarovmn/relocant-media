from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session

from relocant_media.ingestion.persistence import sync_source
from relocant_media.ops.primary_cycle import begin_window, run_cycle
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import EditorialRun, RawItem
from relocant_media.storage.sqlite_schema import initialize_sqlite

editorial_session = _editorial_session
NOW = datetime(2026, 10, 20, 8, tzinfo=UTC)


@pytest.fixture(params=["postgresql", "sqlite"])
def queue(request, tmp_path):
    sqlite_engine = None
    if request.param == "sqlite":
        sqlite_engine = create_engine(f"sqlite:///{tmp_path / 'primary.sqlite3'}")
        initialize_sqlite(tmp_path / "primary.sqlite3")
        session = Session(sqlite_engine)
    else:
        session = request.getfixturevalue("editorial_session")
    session.execute(delete(SchedulerRun).where(SchedulerRun.task_type == "editorial_primary"))
    source = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
    sync_source(session, "belgrade", source)
    for index in range(12):
        session.add(RawItem(source_id=source.id, external_id=str(index),
                            url=f"https://example.test/{index}", title=f"Event {index}",
                            body=f"Separate news body {index}",
                            published_at=NOW - timedelta(hours=1)))
    session.commit()
    engine = session.get_bind()

    def process(raw_id):
        with Session(engine) as worker:
            worker.add(EditorialRun(raw_item_id=raw_id, outcome={"status": "skipped"},
                                    evidence={}, delivery_status="pending"))
            worker.commit()
        return {"transport": "fake", "raw_item_id": raw_id}

    yield engine, process
    if sqlite_engine is not None:
        session.close()
        sqlite_engine.dispose()


def ingest_ok():
    return {"failures": 0, "raw_items_written": 0, "review_required": 0}


def test_twelve_candidates_are_drained_in_three_windows_without_losing_tail(queue, tmp_path):
    engine, process = queue
    reports = [run_cycle(engine, city="belgrade", output=tmp_path / str(i),
                         now=NOW + timedelta(hours=i * 3), ingest=ingest_ok, process=process)
               for i in range(3)]
    assert [r["status"] for r in reports] == ["PRIMARY_PARTIAL", "PRIMARY_PARTIAL", "PRIMARY_OK"]
    assert [r["remaining_count"] for r in reports] == [7, 2, 0]
    assert len({r["window"]["until"] for r in reports}) == 1
    processed = [raw for report in reports for raw in report["processed_raw_item_ids"]]
    assert len(processed) == len(set(processed)) == 12
    next_run = run_cycle(engine, city="belgrade", output=tmp_path / "next",
                         now=NOW + timedelta(hours=9), ingest=ingest_ok, process=process)
    assert next_run["window"]["since"] == (NOW - timedelta(minutes=10)).isoformat()
    assert next_run["status"] == "PRIMARY_OK"


def test_failed_batch_resumes_same_window_and_skips_already_committed_items(queue, tmp_path):
    engine, process = queue
    calls = []

    def interrupted(raw_id):
        calls.append(raw_id)
        if len(calls) == 3:
            raise RuntimeError("sensitive API text must not be logged")
        return process(raw_id)

    failed = run_cycle(engine, city="belgrade", output=tmp_path / "failed", now=NOW,
                       ingest=ingest_ok, process=interrupted)
    assert failed["status"] == "PRIMARY_FAILED"
    assert "sensitive" not in (tmp_path / "failed" / "heartbeat.json").read_text()
    next_run = run_cycle(engine, city="belgrade", output=tmp_path / "retry",
                         now=NOW + timedelta(hours=3), ingest=ingest_ok, process=process)
    assert next_run["window"] == failed["window"]
    assert not set(calls[:2]) & set(next_run["processed_raw_item_ids"])
    assert next_run["remaining_count"] == 5


def test_failed_ingestion_cannot_advance_checkpoint_or_call_ai(queue, tmp_path):
    engine, _ = queue

    def forbidden(raw_id):
        raise AssertionError("must not process a failed collection")

    report = run_cycle(engine, city="belgrade", output=tmp_path / "failed", now=NOW,
                       ingest=lambda: {"failures": 1}, process=forbidden)
    assert report["status"] == "PRIMARY_FAILED" and not report["window_complete"]
    with Session(engine) as session:
        retry = begin_window(session, city="belgrade", now=NOW + timedelta(hours=3))
        assert retry.details["window"] == report["window"]


def test_running_checkpoint_survives_worker_crash(queue):
    engine, _ = queue
    with Session(engine) as session:
        interrupted = begin_window(session, city="belgrade", now=NOW)
        interrupted_window = interrupted.details["window"]
    with Session(engine) as session:
        resumed = begin_window(session, city="belgrade", now=NOW + timedelta(hours=6))
        assert resumed.details["window"] == interrupted_window


def test_parallel_primary_cannot_process_same_queue(queue, tmp_path):
    engine, process = queue
    entered, proceed = Event(), Event()

    def paused_ingest():
        entered.set()
        assert proceed.wait(5)
        return ingest_ok()

    with ThreadPoolExecutor(max_workers=1) as pool:
        active = pool.submit(run_cycle, engine, city="belgrade", output=tmp_path / "first",
                             now=NOW, ingest=paused_ingest, process=process)
        assert entered.wait(5)
        busy = run_cycle(engine, city="belgrade", output=tmp_path / "second", now=NOW,
                         ingest=ingest_ok, process=process)
        assert busy["status"] == "PRIMARY_BUSY"
        proceed.set()
        assert active.result(timeout=5)["remaining_count"] == 7
    # The dedicated connection's session lock was released, not returned locked to the pool.
    assert run_cycle(engine, city="belgrade", output=tmp_path / "third",
                     now=NOW + timedelta(hours=3), ingest=ingest_ok,
                     process=process)["remaining_count"] == 2


def test_corrupted_checkpoint_does_not_restart_from_last_day(queue, tmp_path):
    engine, process = queue
    with Session(engine) as session:
        run = begin_window(session, city="belgrade", now=NOW)
        run.details = {}
        session.commit()
    with pytest.raises(KeyError):
        run_cycle(engine, city="belgrade", output=tmp_path / "corrupt", now=NOW,
                  ingest=ingest_ok, process=process)


def test_primary_cli_missing_database_stops_before_importing_ai(monkeypatch):
    import sys

    from relocant_media.ops.primary_cycle import main

    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["primary", "--output", "unused"])
    with pytest.raises(SystemExit, match="1"):
        main()


def test_session_lock_survives_committed_candidate(queue, tmp_path):
    engine, process = queue

    def process_with_competitor(raw_id):
        result = process(raw_id)  # commits on a separate session
        busy = run_cycle(engine, city="belgrade", output=tmp_path / f"busy-{raw_id}",
                         now=NOW, ingest=ingest_ok, process=process)
        assert busy["status"] == "PRIMARY_BUSY"
        return result

    report = run_cycle(engine, city="belgrade", output=tmp_path / "active", now=NOW,
                       ingest=ingest_ok, process=process_with_competitor)
    assert report["remaining_count"] == 7


def test_late_discovered_old_article_reenters_after_success(queue, tmp_path):
    engine, process = queue
    for i in range(3):
        run_cycle(engine, city="belgrade", output=tmp_path / str(i),
                  now=NOW + timedelta(hours=i * 3), ingest=ingest_ok, process=process)
    with Session(engine) as session:
        original = session.scalar(select(RawItem))
        item = RawItem(source_id=original.source_id, external_id="late", title="Late discovery",
                       body="Old dated article discovered late", url="https://example.test/late",
                       published_at=NOW - timedelta(days=3))
        session.add(item)
        session.commit()
        raw_id = item.id
    report = run_cycle(engine, city="belgrade", output=tmp_path / "late",
                       now=NOW + timedelta(hours=9), ingest=ingest_ok, process=process)
    assert report["processed_raw_item_ids"] == [raw_id]
    assert report["status"] == "PRIMARY_OK"
