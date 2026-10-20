import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ingestion.persistence import sync_source
from relocant_media.ops.local_cycle import run_local_cycle
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import CollectorFailure
from relocant_media.storage.primary_lock import primary_lock
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def local(tmp_path):
    database = tmp_path / "editorial.sqlite3"
    initialize_sqlite(database)
    return dict(database=database, output=tmp_path / "run", city="belgrade",
                since="2026-10-19T00:00:00+00:00", until="2026-10-20T00:00:00+00:00")


def test_ready_export_is_not_review_or_remote_backup(local):
    report = run_local_cycle(**local, ingest=lambda: {"failures": 0})
    assert report["status"] == "LOCAL_READY"
    assert report["backup_ready"]
    assert report["remote_backup"] == report["editorial_review"] == "pending"
    assert report["telegram_send"] is False
    assert json.loads((local["output"] / "manifest.json").read_text())["version"] == 2


def test_failure_after_commit_is_backed_up_and_secret_not_logged(local):
    def ingest():
        engine = create_engine("sqlite:///" + local["database"].as_posix())
        try:
            with Session(engine) as session:
                source = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
                sync_source(session, "belgrade", source)
                session.add(CollectorFailure(source_id=source.id, strategy="test",
                                             error_type="test", message="safe"))
                session.commit()
        finally:
            engine.dispose()
        raise RuntimeError("secret-value")

    report = run_local_cycle(**local, ingest=ingest)
    assert report["status"] == "LOCAL_FAILED"
    assert report["backup_ready"]
    assert "secret-value" not in (local["output"] / "report.json").read_text()
    with sqlite3.connect(local["output"] / "snapshot/editorial.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM collector_failure").fetchone()[0] == 1


def test_partial_collection_still_exports_without_success(local):
    report = run_local_cycle(**local, ingest=lambda: {"failures": 1})
    assert report["status"] == "LOCAL_PARTIAL"
    assert report["backup_ready"]
    assert (local["output"] / "manifest.json").is_file()


def test_busy_cycle_has_no_ingestion_or_output(local):
    engine = create_engine("sqlite:///" + local["database"].as_posix())
    try:
        with primary_lock(engine, 42):
            report = run_local_cycle(**local, ingest=lambda: pytest.fail("busy ingestion"))
        assert report["status"] == "LOCAL_BUSY"
        assert not local["output"].exists()
    finally:
        engine.dispose()


def test_backup_failure_cannot_report_ready(local, monkeypatch):
    def fail(*args):
        raise OSError("secret-value")

    monkeypatch.setattr("relocant_media.ops.local_cycle.create_snapshot", fail)
    report = run_local_cycle(**local, ingest=lambda: {"failures": 0})
    assert report["status"] == "LOCAL_FAILED"
    assert not report["backup_ready"]
    assert report["backup_error_type"] == "OSError"


def test_missing_database_is_not_created(local):
    local["database"].unlink()
    with pytest.raises(ValueError):
        run_local_cycle(**local, ingest=lambda: pytest.fail("missing ingestion"))
    assert not local["database"].exists()
