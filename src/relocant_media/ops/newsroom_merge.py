"""Merge a public runtime projection into a fresh catalog without changing its observation date."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def rows(value: Any, keys: tuple[str, ...]) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("snapshot rows must be a list")
    return [{key: row.get(key) for key in keys} for row in value if isinstance(row, dict)]


def merge(catalog: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    if (runtime.get("schema_version") != 1 or runtime.get("mode") != "public_read_only"
            or not isinstance(runtime.get("cities"), list)):
        raise ValueError("unsupported runtime snapshot")
    for city in catalog["cities"]:
        recorded = next((item for item in runtime["cities"] if item.get("id") == city["id"]), None)
        if recorded is None:
            continue
        # Ignore unknown top-level and per-city fields; catalog definitions come from main.
        for key in ("runtime_status", "observed_at", "counts", "total_events", "returned_events",
                    "items", "runs", "ai_usage", "last_cycle"):
            if key in recorded:
                city[key] = recorded[key]
        if city.get("items") is not None:
            city["items"] = rows(city["items"], (
                "event_key", "state", "reason_code", "raw_item_ids", "published_at",
                "review_status", "delivery_status", "post_id", "telegram_message_id",
                "editorial_rule_version", "source_revision_count", "title", "source_id",
                "discovered_by", "editor_model", "reviews",
            ))
            for event in city["items"]:
                event["reviews"] = rows(event.get("reviews", []), (
                    "role", "verdict", "score", "reviewed_at", "live_source_checked",
                )) or []
        city["runs"] = rows(city.get("runs"), (
            "id", "status", "started_at", "finished_at", "error_type",
        ))
        city["ai_usage"] = rows(city.get("ai_usage"), (
            "provider", "model", "task", "status", "occurred_at", "input_tokens", "output_tokens",
        ))
        cycle = city.get("last_cycle")
        if isinstance(cycle, dict):
            safe_cycle: dict[str, Any] = {
                key: cycle.get(key) for key in ("status", "observed_at", "error_type")
            }
            safe_cycle["ai_calls"] = rows(cycle.get("ai_calls", []), (
                "provider", "model", "task", "status", "occurred_at", "duration_seconds",
            ))
            jev = cycle.get("jev")
            safe_cycle["jev"] = None
            if isinstance(jev, dict):
                safe_cycle["jev"] = {key: jev.get(key) for key in (
                    "status", "mode", "decision_effect", "evaluated", "model",
                )}
                safe_cycle["jev"]["signals"] = rows(jev.get("signals", []), (
                    "raw_item_id", "local_relevance_probability", "practical_value_probability",
                ))
            city["last_cycle"] = safe_cycle
        by_id = {source["id"]: source for source in recorded.get("sources", [])}
        for source in city["sources"]:
            old = by_id.get(source["id"])
            if old:
                source["runtime_enabled"] = old.get("runtime_enabled")
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text())
    runtime = json.loads(args.runtime.read_text())
    args.catalog.write_text(json.dumps(merge(catalog, runtime), ensure_ascii=False, indent=2),
                           encoding="utf-8")


if __name__ == "__main__":
    main()
