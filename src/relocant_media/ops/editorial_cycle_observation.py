"""Report a bounded, sanitized decision summary from a private Actions snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from relocant_media.domain.quiet_hours import QUIET_END, QUIET_START, QUIET_ZONE
from relocant_media.ops.collection_timing import COLLECTION_GROUPS
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient

_TOKEN = re.compile(r"[a-zA-Z0-9_.:-]{1,80}")
_PRIMARY_STAGES = ("collection", "candidate_selection", "batch_preselection", "material_review",
                   "remaining_scan")


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _reported_decision(
    status: object, reason: object, delivery: object, has_post: bool,
    previous_status: object, previous_reason: object,
) -> tuple[str | None, str | None]:
    if (
        status == "blocked" and reason == "lifecycle_separate_publication_approval_required"
        and delivery == "pending" and not has_post
        and previous_status == "skipped" and previous_reason == "ranking_rejected"
    ):
        return "skipped", "ranking_rejected"
    return _token(status), _token(reason)


def _safe_durations(value: object, names: tuple[str, ...]) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        name: round(seconds, 3) for name in names
        if isinstance(seconds := value.get(name), (int, float))
        and not isinstance(seconds, bool) and 0 <= seconds <= 24 * 60 * 60
    }


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
            """SELECT post.telegram_message_id, post.category, editorial_run.outcome
               FROM post JOIN editorial_run ON editorial_run.post_id = post.id
               WHERE post.city = 'belgrade' AND editorial_run.delivery_status = 'sent'
                 AND post.telegram_message_id IS NOT NULL
                 AND julianday(post.published_at) >= julianday(?)
                 AND julianday(post.published_at) <= julianday(?)
               ORDER BY post.published_at, post.id""", bounds,
        ).fetchall()
        unconfirmed = []
        for message_id, category, raw in rows:
            outcome = json.loads(raw)
            receipt = outcome.get("_rich_delivery") or {}
            if category in {"events", "transport", "utilities"} and not receipt.get("map_url"):
                unconfirmed.append(message_id)
        decisions = db.execute(
            """SELECT json_extract(editorial_run.outcome, '$.status'),
                      json_extract(editorial_run.outcome, '$.reason'),
                      editorial_run.delivery_status, editorial_run.post_id IS NOT NULL,
                      json_extract(editorial_run.outcome, '$._lifecycle_block.previous_status'),
                      json_extract(editorial_run.outcome, '$._lifecycle_block.previous_reason'),
                      COUNT(*)
               FROM editorial_run JOIN raw_item ON raw_item.id = editorial_run.raw_item_id
               JOIN source ON source.id = raw_item.source_id
               WHERE source.city = 'belgrade'
                 AND julianday(editorial_run.created_at) >= julianday(?)
                 AND julianday(editorial_run.created_at) <= julianday(?)
               GROUP BY 1, 2, 3, 4, 5, 6""", bounds,
        ).fetchall()
        decision_counts: Counter[tuple[str | None, str | None]] = Counter()
        for status, reason, delivery, has_post, before_status, before_reason, count in decisions:
            decision = _reported_decision(
                status, reason, delivery, bool(has_post), before_status, before_reason,
            )
            decision_counts[decision] += count
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
    return {
        "local_date": local.date().isoformat(), "timezone": QUIET_ZONE.key,
        "standalone_published_count": len(rows),
        "published_message_ids": [row[0] for row in rows],
        "map_button_delivery_unconfirmed": unconfirmed,
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
        "decisions": [{"status": status, "reason": reason, "count": count}
                      for (status, reason), count in decision_counts.most_common(20)],
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
                """SELECT raw_item.source_id, editorial_run.outcome, editorial_run.post_id,
                          editorial_run.delivery_status, post.telegram_message_id, post.category
                   FROM raw_item
                   LEFT JOIN editorial_run ON editorial_run.raw_item_id = raw_item.id
                   LEFT JOIN post ON post.id = editorial_run.post_id
                   WHERE raw_item.id = ?""",
                (raw_id,),
            ).fetchone()
            if row is None:
                continue
            source, raw_outcome, post_id, delivery, message_id, category = row
            outcome = json.loads(raw_outcome) if raw_outcome else {}
            before = outcome.get("_lifecycle_block")
            previous = before if isinstance(before, dict) else {}
            decision_status, decision_reason = _reported_decision(
                outcome.get("status"), outcome.get("reason"), delivery, post_id is not None,
                previous.get("previous_status"), previous.get("previous_reason"),
            )
            presentation = (outcome.get("_package") or {}).get("presentation") or {}
            native_map = bool(presentation.get("native_map"))
            source_map = bool(presentation.get("source_map"))
            has_button = bool((outcome.get("_rich_delivery") or {}).get("map_url"))
            map_format = ("mini_app" if has_button else "telegram_html" if native_map else
                          "source_link" if source_map else None)
            materials.append({
                "raw_item_id": raw_id, "source_id": _token(source),
                "status": decision_status, "reason": decision_reason,
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
