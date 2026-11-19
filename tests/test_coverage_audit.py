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


def test_lifecycle_decisions_aliases_and_receipts_are_projected_without_private_data(
    tmp_path: Path,
) -> None:
    database = tmp_path / "lifecycle.sqlite3"
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
             for i in range(1, 9)])
        connection.executemany("INSERT INTO editorial_run VALUES (?, ?, ?, ?, ?)", [
            (2, json.dumps({"status": "skipped", "reason": "ranking_rejected"}),
             "2026-11-18 12:11:00", "pending", None),
            (4, json.dumps({"status": "ready"}), "2026-11-18 12:12:00", "sent", 10),
            (8, json.dumps({"status": "ready"}), "2026-11-18 12:12:00", "uncertain", 10),
        ])
        connection.execute("INSERT INTO post VALUES (10, '2026-11-18 12:30:00', 135)")
        blocked = {"state": "blocked", "next_action": None, "next_attempt_at": None,
                   "deadline_at": "2026-11-18T18:00:00+00:00", "attempts": 2,
                   "fallback_attempts": 1, "audit_attempts": 1, "delivery_attempts": 0,
                   "rejection_audits": 1, "reason": "attempt_budget_exhausted",
                   "policy_version": "material-lifecycle-v1",
                   "history": [{"prompt": "PRIVATE_HISTORY"}],
                   "evidence": "PRIVATE_EVIDENCE"}
        pending = {**blocked, "state": "waiting", "next_action": "audit_rejection",
                   "next_attempt_at": "2026-11-18T12:15:00+00:00",
                   "reason": "rejection_requires_audit", "rejection_audits": 0}
        details = [
            {**blocked, "raw_item_id": 1},
            {**blocked, "raw_item_id": 2},
            {**pending, "raw_item_id": 2},
            {"raw_item_id": 3, "lifecycle_root_raw_id": 1, "prompts": "PRIVATE_ALIAS"},
            {**blocked, "raw_item_id": 4},
            {"raw_item_id": 5, "lifecycle_root_raw_id": 2},
            {"raw_item_id": 6, "lifecycle_root_raw_id": 999},
            {"raw_item_id": 7, "state": {"private": "PRIVATE_STATE"},
             "next_action": ["PRIVATE_ACTION"], "reason": {"prompt": "PRIVATE_REASON"},
             "policy_version": {"prompt": "PRIVATE_POLICY"}, "attempts": True,
             "deadline_at": {"evidence": "PRIVATE_DATE"}},
            {**blocked, "raw_item_id": 8},
            {**blocked, "raw_item_id": 111, "reason": "FOREIGN_DETAILS"},
        ]
        connection.executemany("INSERT INTO scheduler_run VALUES (?, ?, ?, 'material_lifecycle')",
            [(i, "2026-11-18 12:15:00", json.dumps(item))
             for i, item in enumerate(details, start=1)])
    original = database.read_bytes()
    report = audit_coverage(database, [{"label": "Путь материалов", "urls": [
        f"https://news.example/{i}" for i in range(1, 9)
    ]}], observed_at=datetime(2026, 11, 18, 18, tzinfo=UTC))
    matches = report["cases"][0]["matches"]
    assert [item["stage"] for item in matches] == [
        "blocked", "lifecycle_pending", "blocked", "published", "lifecycle_pending",
        "blocked", "collected_without_decision", "published",
    ]
    assert matches[0]["first_decision_record_at"] is None
    assert matches[0]["lifecycle"]["reason"] == "attempt_budget_exhausted"
    assert matches[1]["decision_status"] == "skipped"
    assert matches[1]["lifecycle"]["next_action"] == "audit_rejection"
    assert matches[1]["lifecycle"]["next_attempt_at"] == "2026-11-18T12:15:00+00:00"
    assert matches[2]["lifecycle"] == matches[0]["lifecycle"]
    assert matches[4]["lifecycle"] == matches[1]["lifecycle"]
    assert matches[5]["lifecycle"]["reason"] == "invalid_lifecycle_link"
    assert "attempts" not in matches[6]["lifecycle"]
    assert not ({"history", "evidence", "raw_item_id", "lifecycle_root_raw_id"}
                & matches[0]["lifecycle"].keys())
    assert "PRIVATE_" not in json.dumps(report)
    assert "FOREIGN_DETAILS" not in json.dumps(report)
    assert database.read_bytes() == original


def test_lifecycle_alias_cycle_is_bounded_and_reported_as_blocked(tmp_path: Path) -> None:
    database = tmp_path / "cycle.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE raw_item (id INTEGER, source_id TEXT, url TEXT, published_at TEXT,
                published_at_precision TEXT, ingested_at TEXT);
            CREATE TABLE editorial_run (raw_item_id INTEGER, outcome TEXT, created_at TEXT,
                delivery_status TEXT, post_id INTEGER);
            CREATE TABLE post (id INTEGER, published_at TEXT, telegram_message_id INTEGER);
            CREATE TABLE scheduler_run (id INTEGER, started_at TEXT, details TEXT, task_type TEXT);
            INSERT INTO raw_item VALUES (1, 'media', 'https://news.example/1',
                '2026-11-18 12:00:00', 'instant', '2026-11-18 12:10:00');
        """)
        connection.executemany("INSERT INTO scheduler_run VALUES (?, ?, ?, 'material_lifecycle')",
            [(raw_id, "2026-11-18 12:15:00", json.dumps({
                "raw_item_id": raw_id, "lifecycle_root_raw_id": target,
            })) for raw_id, target in ((1, 2), (2, 1))])
    report = audit_coverage(database, [{"label": "Цикл", "urls": ["https://news.example/1"]}],
                            observed_at=datetime(2026, 11, 18, 18, tzinfo=UTC))
    match = report["cases"][0]["matches"][0]
    assert match["stage"] == "blocked"
    assert match["lifecycle"]["reason"] == "invalid_lifecycle_link"
