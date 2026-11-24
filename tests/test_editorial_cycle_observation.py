import json
import sqlite3
from datetime import UTC, datetime

import pytest

from relocant_media.ops.editorial_cycle_observation import observe_cycle, observe_day


def test_daily_audit_uses_belgrade_day_and_keeps_only_sanitized_public_status(tmp_path):
    snapshot = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(snapshot) as db:
        db.executescript("""
            CREATE TABLE post(id, city, telegram_message_id, category, published_at);
            CREATE TABLE source(id, city);
            CREATE TABLE raw_item(id, source_id);
            CREATE TABLE editorial_run(post_id, raw_item_id, delivery_status, outcome, created_at);
            CREATE TABLE scheduler_run(id, task_key, status, details, started_at, city, task_type);
            CREATE TABLE submission(id, kind, payload, status, created_at);
            INSERT INTO source VALUES ('a', 'belgrade');
        """)
        reader = {"reader_intake": True, "text": "PRIVATE reader text",
                  "reader_publication": {"ownership": "own", "permission": "allow"}}
        for ident, payload, status in [
            (1, reader, "reader_review"),
            (2, reader, "reader_review"),
            (3, {**reader, "reader_resolution": {"outcome": "published"}}, "closed"),
            (4, {**reader, "test_only": True}, "reader_review"),
            (5, {"text": "PRIVATE channel tip"}, "waiting_primary"),
        ]:
            db.execute("INSERT INTO submission VALUES (?, 'news_tip', ?, ?, ?)",
                       (ident, json.dumps(payload), status, "2026-11-17T08:00:00+00:00"))
        # Полночь Белграда = 23:00 UTC накануне; вчерашний пост не входит.
        for number, published, category, outcome in [
            (125, "2026-11-16T22:59:00+00:00", "events", {}),
            (126, "2026-11-16T23:01:00+00:00", "transport", {}),
            (128, "2026-11-17T10:36:00+00:00", "events",
             {"_rich_delivery": {"map_url": "https://t.me/bot?startapp=places_example"}}),
            (130, "2026-11-18T00:00:00+00:00", "events", {}),
        ]:
            db.execute("INSERT INTO post VALUES (?, 'belgrade', ?, ?, ?)",
                       (number, number, category, published))
            db.execute("INSERT INTO raw_item VALUES (?, 'a')", (number,))
            db.execute("INSERT INTO editorial_run VALUES (?, ?, 'sent', ?, ?)",
                       (number, number, json.dumps(outcome), published))
        db.execute("INSERT INTO raw_item VALUES (999, 'a')")
        db.execute("INSERT INTO editorial_run VALUES (NULL, 999, 'pending', ?, ?)", (
            json.dumps({"status": "human_review", "reason": "PRIVATE TOKEN:secret"}),
            "2026-11-17T08:00:00+00:00",
        ))
        db.execute("""
            INSERT INTO scheduler_run VALUES
            (1, 'editorial-primary:belgrade', ?, ?, ?, 'belgrade', 'editorial_primary')
        """, (
            "partial", json.dumps({"gpt_deferred_count": 6, "remaining_count": 100}),
            "2026-11-17T20:00:00+00:00",
        ))
        for ident, raw, date, city, status, reason, replacement in [
            (2, 500, "2026-11-16T22:15:00Z", "belgrade", "delivered", "sent", 128),
            (3, 501, "2026-11-17T02:25:00Z", "belgrade", "blocked",
             "material_deadline_exceeded", None),
            (4, 999, "2026-11-17T06:59:59Z", "belgrade", "ready", "waiting", None),
            (5, 503, "2026-11-17T07:00:00Z", "belgrade", "ready", "waiting", None),
            (6, 504, "2026-11-17T01:57:00Z", "belgrade", "ready", "waiting", None),
            (7, 505, "2026-11-17T02:25:00Z", "novi_sad", "ready", "waiting", None),
            (8, 504, "2026-11-17T07:00:00Z", "belgrade", "ready", "waiting", None),
            (9, 506, "2026-11-16T21:59:59Z", "belgrade", "ready", "waiting", None),
        ]:
            db.execute("""
                INSERT INTO scheduler_run VALUES (?, ?, ?, ?, ?, ?, 'material_lifecycle')
            """, (ident, f"material-lifecycle:{city}:{raw}", status, json.dumps({
                "raw_item_id": raw, "reason": reason, "replacement_raw_item_id": replacement,
                "morning_hold": {"at": date, "private": "PRIVATE"},
            }), date, city))
    report = observe_day(snapshot, datetime(2026, 11, 17, 21, tzinfo=UTC))
    assert report["local_date"] == "2026-11-17"
    assert report["standalone_published_count"] == 2
    assert report["published_message_ids"] == [126, 128]
    assert report["map_button_delivery_unconfirmed"] == [126]
    assert report["gpt_deferred_count"] == 6
    assert report["morning_hold"] == {
        "materials": 3, "published_message_ids": [128], "deadline_exceeded": 1,
    }
    before_dawn = observe_day(snapshot, datetime(2026, 11, 17, 1, tzinfo=UTC))
    assert before_dawn["morning_hold"] == {
        "materials": 1, "published_message_ids": [], "deadline_exceeded": 0,
    }
    assert "PRIVATE" not in json.dumps(report)
    assert report["reader_submissions_30d"] == [
        {"status": "reader_review", "ownership": "own", "permission": "allow",
         "outcome": None, "count": 2},
        {"status": "closed", "ownership": "own", "permission": "allow",
         "outcome": "published", "count": 1},
    ]
    assert {"status": "human_review", "reason": None, "count": 1} in report["decisions"]
    with pytest.raises(ValueError, match="timezone"):
        observe_day(snapshot, datetime(2026, 11, 17))


def test_cycle_observation_exposes_only_bounded_stage_measurements(tmp_path):
    snapshot = tmp_path / "snapshot.sqlite3"
    with sqlite3.connect(snapshot) as db:
        db.execute("CREATE TABLE scheduler_run(id, task_key, status, details, started_at)")
        start = "2026-11-17T12:00:00Z"
        db.execute("INSERT INTO scheduler_run VALUES (1, 'editorial-primary:belgrade', ?, ?, ?)", (
            "partial", json.dumps({"stage_seconds": {
                "collection": 12.7, "material_review": 0, "candidate_selection": -2,
                "batch_preselection": True, "remaining_scan": 100_000,
                "PRIVATE source": 3,
            }}), start,
        ))
    report = observe_cycle(snapshot, start)
    assert report["stage_seconds"] == {"collection": 12.7, "material_review": 0}
