"""Report a bounded, sanitized decision summary from a private Actions snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from relocant_media.domain.event_map_policy import event_map_review_reason
from relocant_media.domain.map_links import is_map_event_url
from relocant_media.domain.quiet_hours import QUIET_END, QUIET_START, QUIET_ZONE
from relocant_media.ops.collection_timing import COLLECTION_GROUPS
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient

_TOKEN = re.compile(r"[a-zA-Z0-9_.:-]{1,80}")
_PRIMARY_STAGES = ("collection", "candidate_selection", "batch_preselection", "material_review",
                   "remaining_scan")


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _safe_durations(value: object, names: tuple[str, ...]) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        name: round(seconds, 3) for name in names
        if isinstance(seconds := value.get(name), (int, float))
        and not isinstance(seconds, bool) and 0 <= seconds <= 24 * 60 * 60
    }


def _confirmed_map_button(raw: str, message_id: int) -> bool:
    receipt = json.loads(raw).get("_rich_delivery")
    if not isinstance(receipt, dict):
        return False
    url = receipt.get("map_url")
    return (receipt.get("state") == "sent" and receipt.get("message_ids") == [message_id]
            and isinstance(url, str) and is_map_event_url(url))


def observe_day(snapshot: Path, now: datetime) -> dict[str, Any]:
    """Аудит календарного дня: только публичные номера, счётчики и коды причин.

    Отсутствие записи о кнопке означает необходимость проверки, а не доказанное
    отсутствие кнопки: старые кнопки могли быть добавлены отдельным backfill.
    """
    if now.tzinfo is None:
        raise ValueError("audit time must include timezone")
    local = now.astimezone(QUIET_ZONE)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    bounds = (start.astimezone(UTC).isoformat(), min(now, end).astimezone(UTC).isoformat())
    evening = datetime.combine(local.date() - timedelta(days=1), QUIET_START, QUIET_ZONE)
    morning = datetime.combine(local.date(), QUIET_END, QUIET_ZONE)
    evening_stamp = evening.astimezone(UTC).isoformat()
    morning_stamp = morning.astimezone(UTC).isoformat()
    uri = snapshot.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        rows = db.execute(
            """SELECT post.telegram_message_id, post.category, editorial_run.outcome,
                      post.title, post.body
               FROM post JOIN editorial_run ON editorial_run.post_id = post.id
               WHERE post.city = 'belgrade' AND editorial_run.delivery_status = 'sent'
                 AND post.telegram_message_id IS NOT NULL
                 AND julianday(post.published_at) >= julianday(?)
                 AND julianday(post.published_at) <= julianday(?)
               ORDER BY post.published_at, post.id""", bounds,
        ).fetchall()
        map_posts: dict[int, tuple[str, bool]] = {}
        for message_id, category, raw, title, body in rows:
            if category in {"transport", "utilities"} or event_map_review_reason(
                None, category, title, body, current_delivery=True, city="belgrade",
            ) is not None:
                _, already_confirmed = map_posts.get(message_id, (category, False))
                map_posts[message_id] = (
                    category, already_confirmed or _confirmed_map_button(raw, message_id),
                )
        decisions = db.execute(
            """SELECT json_extract(editorial_run.outcome, '$.status'),
                      json_extract(editorial_run.outcome, '$.reason'), COUNT(*)
               FROM editorial_run JOIN raw_item ON raw_item.id = editorial_run.raw_item_id
               JOIN source ON source.id = raw_item.source_id
               WHERE source.city = 'belgrade'
                 AND julianday(editorial_run.created_at) >= julianday(?)
                 AND julianday(editorial_run.created_at) <= julianday(?)
               GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20""", bounds,
        ).fetchall()
        held = db.execute(
            """SELECT lifecycle.status, json_extract(lifecycle.details, '$.reason'),
                      post.telegram_message_id
               FROM scheduler_run AS lifecycle
               LEFT JOIN editorial_run ON editorial_run.raw_item_id = COALESCE(
                 json_extract(lifecycle.details, '$.replacement_raw_item_id'),
                 json_extract(lifecycle.details, '$.raw_item_id')
               ) AND editorial_run.delivery_status = 'sent'
               LEFT JOIN post ON post.id = editorial_run.post_id AND post.city = 'belgrade'
                 AND julianday(post.published_at) >= julianday(?)
                 AND julianday(post.published_at) <= julianday(?)
               WHERE lifecycle.city = 'belgrade' AND lifecycle.task_type = 'material_lifecycle'
                 AND julianday(json_extract(lifecycle.details, '$.morning_hold.at')) >= julianday(?)
                 AND julianday(json_extract(lifecycle.details, '$.morning_hold.at')) < julianday(?)
                 AND julianday(json_extract(lifecycle.details, '$.morning_hold.at')) <= julianday(?)
                 AND NOT EXISTS (
                   SELECT 1 FROM scheduler_run AS newer
                   WHERE newer.task_key = lifecycle.task_key AND newer.id > lifecycle.id
                 )""",
            (*bounds, evening_stamp, morning_stamp, bounds[1]),
        ).fetchall()
        cycle = db.execute(
            """SELECT status, details FROM scheduler_run
               WHERE task_key = 'editorial-primary:belgrade'
                 AND julianday(started_at) <= julianday(?) ORDER BY id DESC LIMIT 1""",
            (now.astimezone(UTC).isoformat(),),
        ).fetchone()
        details = json.loads(cycle[1]) if cycle else {}
        reader = db.execute(
            """SELECT status,
                      json_extract(payload, '$.reader_publication.ownership'),
                      json_extract(payload, '$.reader_publication.permission'),
                      COALESCE(json_extract(payload, '$.reader_resolution.outcome'),
                               json_extract(payload, '$.triage_reason')),
                      COUNT(*)
               FROM submission
               WHERE kind = 'news_tip'
                 AND json_extract(payload, '$.reader_intake') = 1
                 AND COALESCE(json_extract(payload, '$.test_only'), 0) = 0
                 AND julianday(created_at) >= julianday(?)
               GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 30""",
            ((now - timedelta(days=30)).astimezone(UTC).isoformat(),),
        ).fetchall()
    unconfirmed = [message_id for message_id, (_, confirmed) in map_posts.items() if not confirmed]
    confirmed_count = len(map_posts) - len(unconfirmed)
    by_category: dict[str, dict[str, int]] = {}
    for category, confirmed in map_posts.values():
        counts = by_category.setdefault(category, {"posts": 0, "confirmed_at_send": 0})
        counts["posts"] += 1
        counts["confirmed_at_send"] += int(confirmed)
    return {
        "local_date": local.date().isoformat(), "timezone": QUIET_ZONE.key,
        "standalone_published_count": len(rows),
        "published_message_ids": [row[0] for row in rows],
        "map_button_delivery_unconfirmed": unconfirmed,
        "map_button_coverage": {
            "posts": len(map_posts),
            "confirmed_at_send": confirmed_count,
            "unconfirmed": len(unconfirmed),
            "confirmed_percent": round(100 * confirmed_count / len(map_posts), 1)
            if map_posts else None,
            "by_category": by_category,
        },
        "morning_hold": {
            "materials": len(held),
            "published_message_ids": sorted({
                number for _, _, number in held if type(number) is int and number > 0
            }),
            "deadline_exceeded": sum(
                status == "blocked" and reason == "material_deadline_exceeded"
                for status, reason, _ in held
            ),
        },
        "decisions": [{"status": _token(status), "reason": _token(reason), "count": count}
                      for status, reason, count in decisions],
        "latest_cycle_status": _token(cycle[0]) if cycle else None,
        "gpt_deferred_count": details.get("gpt_deferred_count", 0),
        "remaining_count": details.get("remaining_count"),
        # Counts only (#470): why reader submissions did or did not reach the channel.
        "reader_submissions_30d": [
            {"status": _token(status), "ownership": _token(ownership),
             "permission": _token(permission), "outcome": _token(outcome), "count": count}
            for status, ownership, permission, outcome, count in reader
        ],
    }


def observe_cycle(snapshot: Path, started_at: str) -> dict[str, Any] | None:
    uri = snapshot.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        cycle = db.execute(
            """SELECT id, status, details FROM scheduler_run
               WHERE task_key = 'editorial-primary:belgrade'
                 AND julianday(started_at) >= julianday(?)
               ORDER BY id DESC LIMIT 1""",
            (started_at,),
        ).fetchone()
        if cycle is None:
            return None
        cycle_id, status, raw_details = cycle
        details = json.loads(raw_details)
        materials = []
        for raw_id in details.get("processed_raw_item_ids", [])[:5]:
            row = db.execute(
                """SELECT raw_item.source_id, editorial_run.outcome,
                          editorial_run.delivery_status, post.telegram_message_id, post.category
                   FROM raw_item
                   LEFT JOIN editorial_run ON editorial_run.raw_item_id = raw_item.id
                   LEFT JOIN post ON post.id = editorial_run.post_id
                   WHERE raw_item.id = ?""",
                (raw_id,),
            ).fetchone()
            if row is None:
                continue
            source, raw_outcome, delivery, message_id, category = row
            outcome = json.loads(raw_outcome) if raw_outcome else {}
            presentation = (outcome.get("_package") or {}).get("presentation") or {}
            native_map = bool(presentation.get("native_map"))
            source_map = bool(presentation.get("source_map"))
            has_button = bool((outcome.get("_rich_delivery") or {}).get("map_url"))
            map_format = ("mini_app" if has_button else "telegram_html" if native_map else
                          "source_link" if source_map else None)
            materials.append({
                "raw_item_id": raw_id, "source_id": _token(source),
                "status": _token(outcome.get("status")), "reason": _token(outcome.get("reason")),
                "rubric": _token(presentation.get("rubric_id")),
                "has_map": native_map or source_map or has_button,
                "map_format": map_format,
                "event_map_needs_review": outcome.get("reason") ==
                "event_place_requires_verification" or (
                    category == "events" and delivery == "sent" and not has_button),
                "illustration": _token((presentation.get("illustration") or {}).get(
                    "illustration_mode")),
                "delivery": _token(delivery), "telegram_message_id": message_id,
            })
        batch = details.get("batch_preselection")
        waiting_for_gpt = isinstance(batch, dict) and batch.get("status") == "gpt_waiting"
        stage_seconds = _safe_durations(details.get("stage_seconds"), _PRIMARY_STAGES)
        collection_seconds = _safe_durations(details.get("collection_seconds"), COLLECTION_GROUPS)
        return {"scheduler_run_id": cycle_id, "status": _token(status),
                "gpt_batch_waiting": waiting_for_gpt,
                **({"stage_seconds": stage_seconds} if stage_seconds else {}),
                **({"collection_seconds": collection_seconds} if collection_seconds else {}),
                "materials": materials, "errors": [
                    {"raw_item_id": error["raw_item_id"], "type": _token(error.get("error_type")),
                     "site": _token(error.get("error_site"))}
                    for error in details.get("processing_errors", [])[:5]
                ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        run_id = os.environ["GITHUB_RUN_ID"]
        if args.receipt is not None:
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            if (receipt.get("run_id") != run_id
                    or receipt.get("run_attempt") != os.environ["GITHUB_RUN_ATTEMPT"]):
                raise ValueError("snapshot has not been committed by this workflow attempt")
            snapshot_sha256 = receipt["snapshot_sha256"]
            started_at = receipt["started_at"]
            if datetime.fromisoformat(started_at).utcoffset() is None:
                raise ValueError("committed snapshot requires a timezone")
        else:
            client = SSHStateClient(SSHConfig.from_environment(os.environ))
            state, _ = client.read()
            if state.get("status") != "ready" or state.get("run_id") != run_id:
                raise ValueError("snapshot has not been committed by this workflow")
            snapshot_sha256 = state["snapshot"]["sha256"]
            started_at = state["started_at"]
        with args.snapshot.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != snapshot_sha256:
                raise ValueError("local snapshot differs from committed state")
        observation = observe_cycle(args.snapshot, started_at)
        day_observation = observe_day(args.snapshot, datetime.now(UTC))
    except Exception as error:
        print(json.dumps({"cycle_observation_error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"editorial_cycle_observation": observation,
                      "editorial_day_observation": day_observation}))


if __name__ == "__main__":
    main()
