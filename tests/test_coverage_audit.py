import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from relocant_media.ops.coverage_audit import audit_coverage


def test_reports_distinct_stages_and_preserves_database(tmp_path: Path) -> None:
    database = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE raw_item (id INTEGER, source_id TEXT, url TEXT, published_at TEXT,
                published_at_precision TEXT, ingested_at TEXT);
            CREATE TABLE editorial_run (raw_item_id INTEGER, outcome TEXT, created_at TEXT,
                delivery_status TEXT, post_id INTEGER);
            CREATE TABLE post (id INTEGER, published_at TEXT, telegram_message_id INTEGER);
            CREATE TABLE scheduler_run (id INTEGER, started_at TEXT, details TEXT, task_type TEXT);
        """)
        connection.executemany("INSERT INTO raw_item VALUES (?, 'media', ?, ?, 'instant', ?)",
            [(i, f"https://news.example/{i}", "2026-11-18 12:00:00", "2026-11-18 12:10:00")
             for i in range(1, 6)])
        connection.executemany("INSERT INTO editorial_run VALUES (?, ?, ?, ?, ?)", [
            (1, json.dumps({"status":"ready", "reason":"reviewed"}),
             "2026-11-18 16:40:00", "sent", 10),
            (2, json.dumps({"status":"skipped", "reason":"ranking_rejected"}),
             "2026-11-18 12:11:00", "pending", None),
            (3, json.dumps({"status":"human_review", "reason":"policy_requires_human_review"}),
             "2026-11-18 12:12:00", "pending", None),
        ])
        connection.execute("INSERT INTO post VALUES (10, '2026-11-18 16:40:03', 135)")
        connection.execute("INSERT INTO scheduler_run VALUES (1, ?, ?, 'editorial_primary')", (
            "2026-11-18 12:15:00", json.dumps({"processing_errors":[
                {"raw_item_id":1, "error_type":"GeminiWorkerError", "secret":"do-not-leak"},
                {"raw_item_id":4, "error_type":"ValueError"}]})))
    original = database.read_bytes()
    report = audit_coverage(database, [{"label":"Проверка", "urls":[
        f"https://news.example/{i}" for i in range(1, 7)
    ]}], observed_at=datetime(2026,11,18,18,tzinfo=UTC))
    matches = report["cases"][0]["matches"]
    assert [r["stage"] for r in matches] == [
        "published", "selection_rejected", "review_hold", "processing_failed",
        "collected_without_decision",
    ]
    assert matches[0]["ingest_to_first_record_or_observation_minutes"] == 270
    assert matches[0]["processing_errors"][0]["error_type"] == "GeminiWorkerError"
    assert matches[-1]["ingest_to_first_record_or_observation_minutes"] == 350
    assert report["cases"][0]["exact_urls_not_found"] == ["https://news.example/6"]
    assert "do-not-leak" not in json.dumps(report)
    assert database.read_bytes() == original
    # Штатный повторный import меняет outcome, не исходное created_at.
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE editorial_run SET outcome=?, delivery_status='sent', "
                           "post_id=10 WHERE raw_item_id=3", (
                               json.dumps({"status":"ready", "reason":"later_reviewed"}),))
    updated = audit_coverage(database, [{"label":"Повторная проверка", "urls":[
        "https://news.example/3",
    ]}], observed_at=datetime(2026,11,18,18,tzinfo=UTC))["cases"][0]["matches"][0]
    assert updated["stage"] == "published"
    assert updated["decision_reason"] == "later_reviewed"
    assert updated["first_decision_record_at"] == "2026-11-18 12:12:00"
    assert updated["delivered_at"] == "2026-11-18 16:40:03"
    assert updated["ingest_to_first_record_or_observation_minutes"] == 2
    assert "decision_at" not in updated


def test_requires_explicit_observation_and_does_not_create_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="часовым поясом"):
        audit_coverage(missing, [{"label":"Проверка", "urls":["https://news.example/1"]}],
                       observed_at=datetime(2026,11,18))
    with pytest.raises(sqlite3.OperationalError):
        audit_coverage(missing, [{"label":"Проверка", "urls":["https://news.example/1"]}],
                       observed_at=datetime(2026,11,18,tzinfo=UTC))
    assert not missing.exists()
