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

from relocant_media.domain.quiet_hours import QUIET_END, QUIET_START, QUIET_ZONE
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient

_TOKEN = re.compile(r"[a-zA-Z0-9_.:-]{1,80}")


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


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
        "decisions": [{"status": _token(status), "reason": _token(reason), "count": count}
                      for status, reason, count in decisions],
        "latest_cycle_status": _token(cycle[0]) if cycle else None,
        "gpt_deferred_count": details.get("gpt_deferred_count", 0),
        "remaining_count": details.get("remaining_count"),
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
        return {"scheduler_run_id": cycle_id, "status": _token(status),
                "gpt_batch_waiting": waiting_for_gpt,
                "materials": materials, "errors": [
                    {"raw_item_id": error["raw_item_id"], "type": _token(error.get("error_type")),
                     "site": _token(error.get("error_site"))}
                    for error in details.get("processing_errors", [])[:5]
                ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()
    try:
        client = SSHStateClient(SSHConfig.from_environment(os.environ))
        state, _ = client.read()
        if state.get("status") != "ready" or state.get("run_id") != os.environ["GITHUB_RUN_ID"]:
            raise ValueError("snapshot has not been committed by this workflow")
        with args.snapshot.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != state["snapshot"]["sha256"]:
                raise ValueError("local snapshot differs from committed state")
        observation = observe_cycle(args.snapshot, state["started_at"])
        day_observation = observe_day(args.snapshot, datetime.now(UTC))
    except Exception as error:
        print(json.dumps({"cycle_observation_error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"editorial_cycle_observation": observation,
                      "editorial_day_observation": day_observation}))


if __name__ == "__main__":
    main()
