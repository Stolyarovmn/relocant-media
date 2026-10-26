"""Public metadata from messenger checks; never copy preview text or private errors."""
from __future__ import annotations

from datetime import datetime
from typing import Any

STATUSES = {
    "not_checked", "dated_messages", "stale_snapshot", "freshness_unknown", "future_dates",
    "landing_only", "unsupported_preview", "access_error", "blocked_no_message_access",
    "catalog_only",
}


def _time(value: Any) -> str | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def public_coverage(value: Any, sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("city") != "belgrade":
        return None
    entries = value.get("sources")
    if not isinstance(entries, list):
        return None
    known = {s["id"]: s["id"] for s in sources}
    for source in sources:
        if source.get("platform") == "telegram":
            known[source["url"].rstrip("/").rsplit("/", 1)[-1].lower()] = source["id"]
    rows = []
    for row in entries[:200]:
        if not isinstance(row, dict):
            continue
        key = row.get("source_id") or row.get("channel", "")
        if not isinstance(key, str):
            continue
        key = key if key in known else key.lower()
        if key not in known:
            continue
        status = row.get("status")
        result: dict[str, Any] = {
            "source_id": known[key], "status": status
            if isinstance(status, str) and status in STATUSES else "not_checked",
            "observed_at": _time(row.get("observed_at") or value.get("observed_at")),
        }
        for name in ("leads", "dated_messages", "freshness_unknown", "stale_messages",
                     "future_messages", "permalink_attempts", "permalink_failures"):
            number = row.get(name)
            result[name] = number if type(number) is int and 0 <= number <= 1_000_000 else None
        rows.append(result)
    return {"city": "belgrade", "observed_at": _time(value.get("observed_at")),
            "interval_coverage": "not_checked" if (value.get("scope") == "submissions_only"
            or value.get("interval_coverage") == "not_checked")
            else "partial", "sources": rows}
