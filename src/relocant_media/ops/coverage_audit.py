"""Локальная проверка пути известных публичных URL; БД открывается только для чтения."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from relocant_media.domain.dedup import canonicalize_url


def _utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    # SQLite хранит UTC без суффикса; внешний observed_at проверяется отдельно.
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def _object(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Ожидался объект сохранённого решения")
    return value


def _lifecycle(connection: sqlite3.Connection, raw_id: int) -> dict[str, Any] | None:
    """Прочитать только безопасные поля root ledger, не меняя старый снимок."""
    seen: set[int] = set()
    for _ in range(10):
        if raw_id in seen:
            break
        seen.add(raw_id)
        row = connection.execute(
            "SELECT details FROM scheduler_run WHERE task_type=? AND json_valid(details) "
            "AND json_extract(details, '$.raw_item_id')=? ORDER BY id DESC LIMIT 1",
            ("material_lifecycle", raw_id),
        ).fetchone()
        if row is None:
            if len(seen) == 1:
                return None
            break
        details = _object(row["details"]) or {}
        if type(details.get("raw_item_id")) is not int or details["raw_item_id"] != raw_id:
            break
        if "lifecycle_root_raw_id" in details:
            root_id = details["lifecycle_root_raw_id"]
            if type(root_id) is not int or root_id <= 0:
                break
            raw_id = root_id
            continue
        result: dict[str, Any] = {}
        status = details.get("state")
        if isinstance(status, str) and status in {
            "waiting", "processing", "ready", "waiting_fallback", "blocked", "published",
        }:
            result["status"] = status
        action = details.get("next_action")
        if action is None or isinstance(action, str) and action in {
            "prepare", "retry_prepare", "repair", "fallback", "process",
            "audit_rejection", "deliver", "owner_delivery",
        }:
            result["next_action"] = action
        for key in ("next_attempt_at", "deadline_at"):
            value = details.get(key)
            if value is None:
                result[key] = None
            elif isinstance(value, str):
                try:
                    result[key] = _utc(value).isoformat()
                except ValueError:
                    pass
        for key in ("attempts", "fallback_attempts", "rejection_audits", "audit_attempts",
                    "delivery_attempts"):
            value = details.get(key)
            if type(value) is int and 0 <= value <= 100:
                result[key] = value
        for key in ("reason", "policy_version"):
            value = details.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
                result[key] = value
        return result
    return {"status": "blocked", "reason": "invalid_lifecycle_link"}


def audit_coverage(
    database: Path, cases: list[dict[str, Any]], *, observed_at: datetime,
) -> dict[str, Any]:
    """Отсутствие точного URL не доказывает отсутствие темы под другой ссылкой."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("Нужен observed_at с часовым поясом")
    if not 1 <= len(cases) <= 30:
        raise ValueError("Нужны 1–30 проверяемых тем")
    wanted: set[str] = set()
    for case in cases:
        if (not isinstance(case.get("label"), str) or not case["label"].strip()
                or not isinstance(case.get("urls"), list) or not 1 <= len(case["urls"]) <= 10):
            raise ValueError("Нужны label и 1–10 urls на тему")
        for url in case["urls"]:
            if not isinstance(url, str) or not url.startswith("https://") or len(url) > 2048:
                raise ValueError("Ожидалась публичная HTTPS-ссылка")
            wanted.add(canonicalize_url(url))
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        raw_ids = {
            row["id"] for row in connection.execute("SELECT id, url FROM raw_item")
            if canonicalize_url(row["url"]) in wanted
        }
        records: dict[str, list[dict[str, Any]]] = {url: [] for url in wanted}
        errors: dict[int, list[dict[str, Any]]] = {}
        for row in connection.execute(
            "SELECT id, started_at, details FROM scheduler_run "
            "WHERE task_type='editorial_primary' ORDER BY id"
        ):
            details = _object(row["details"]) or {}
            for error in details.get("processing_errors", []):
                if not isinstance(error, dict) or error.get("raw_item_id") not in raw_ids:
                    continue
                raw_id = error["raw_item_id"]
                kind = str(error.get("error_type", "Unknown"))
                errors.setdefault(raw_id, []).append({
                    "scheduler_run_id": row["id"], "started_at": row["started_at"],
                    "error_type": kind if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", kind)
                    else "Unknown",
                })
        for raw_id in sorted(raw_ids):
            record = connection.execute(
                "SELECT r.id, r.source_id, r.url, r.published_at, r.published_at_precision, "
                "r.ingested_at, e.outcome, e.created_at AS first_decision_record_at, "
                "e.delivery_status, "
                "p.published_at AS delivered_at, p.telegram_message_id "
                "FROM raw_item r LEFT JOIN editorial_run e ON e.raw_item_id=r.id "
                "LEFT JOIN post p ON p.id=e.post_id WHERE r.id=?", (raw_id,),
            ).fetchone()
            assert record is not None
            value = dict(record)
            outcome = _object(value.pop("outcome"))
            lifecycle = _lifecycle(connection, raw_id)
            if lifecycle is not None:
                value["lifecycle"] = lifecycle
            if (value["delivery_status"] == "sent"
                    or type(value["telegram_message_id"]) is int
                    and value["telegram_message_id"] > 0 and value["delivered_at"]):
                stage = "published"
            elif lifecycle and lifecycle.get("status") == "blocked":
                stage = "blocked"
            elif (lifecycle and lifecycle.get("status") not in {"blocked", "published"}
                    and lifecycle.get("next_action")):
                stage = "lifecycle_pending"
            elif outcome is None:
                stage = "processing_failed" if errors.get(raw_id) else "collected_without_decision"
            elif outcome.get("status") == "skipped":
                stage = "selection_rejected"
            elif outcome.get("status") == "human_review":
                stage = "review_hold"
            else:
                stage = "decision_recorded"
            value.update(stage=stage, decision_status=(outcome or {}).get("status"),
                         decision_reason=(outcome or {}).get("reason"),
                         processing_errors=errors.get(raw_id, []))
            # Повторный import/reassessment может заменить outcome, сохранив created_at.
            # Поэтому это время первой строки, а не момент текущего решения.
            end = (_utc(value["first_decision_record_at"])
                   if value["first_decision_record_at"] else observed_at)
            value["ingest_to_first_record_or_observation_minutes"] = round(
                (end - _utc(value["ingested_at"])).total_seconds()/60, 2,
            )
            records[canonicalize_url(value["url"])].append(value)
    return {
        "version": 1, "observed_at": observed_at.astimezone(UTC).isoformat(),
        "database_access": "read_only", "network_calls": 0,
        "cases": [{"label": case["label"], "matches": [
            row for url in case["urls"] for row in records[canonicalize_url(url)]
        ], "exact_urls_not_found": [url for url in case["urls"]
                                    if not records[canonicalize_url(url)]]} for case in cases],
        "limitations": [
            "Не найдена точная ссылка — тема могла быть собрана под другим URL.",
            "Сохранённое решение может заменять раннюю версию; errors — отдельные записи запусков.",
            "first_decision_record_at — время первой строки; время замены решения неизвестно.",
            "Интервалы относятся к сохранённым UTC-временам, а не к скорости конкурентов.",
            "Содержимое документов, личные заявки и ключи не включены в отчёт.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--observed-at", type=datetime.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Нужен новый путь отчёта")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    report = audit_coverage(args.database, cases, observed_at=args.observed_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"cases": len(report["cases"]), "database_access": "read_only"}))


if __name__ == "__main__":
    main()
