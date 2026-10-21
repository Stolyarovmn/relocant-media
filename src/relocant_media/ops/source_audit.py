"""Audit registered sources without database, model calls or Telegram access."""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relocant_media.collectors.factory import build_collector
from relocant_media.domain.freshness import freshness_decision
from relocant_media.sources.registry import load_source_registry


async def audit_sources(
    directory: Path,
    *,
    online: bool = False,
    source_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    sources = load_source_registry(directory)
    if source_ids:
        if set(source_ids) - {source.id for source in sources}:
            raise ValueError("unknown source id")
        sources = tuple(source for source in sources if source.id in source_ids)

    rows = []
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        for source in sources:
            row: dict[str, Any] = {
                "id": source.id,
                "name": source.name,
                "url": str(source.url),
                "type": source.source_type,
                "strategy": source.extraction.strategy,
                "auto_publish": source.policy.auto_publish,
                "status": "configured" if source.extraction.status == "active" else "planned",
            }
            if row["status"] == "configured" and online:
                try:
                    collector = build_collector(source, client)
                    items = await collector.collect()
                    item_failures = getattr(collector, "item_failures", [])
                    fresh = [
                        item
                        for item in items
                        if freshness_decision(
                            item.title,
                            item.body,
                            item.published_at,
                            datetime.now(UTC),
                        ).action
                        == "keep"
                    ]
                    row.update(
                        status="ok" if fresh else "empty",
                        fetched_items=len(items),
                        fresh_items=len(fresh),
                        examples=[
                            {
                                "title": item.title,
                                "url": item.url,
                                "published_at": (
                                    item.published_at.isoformat() if item.published_at else None
                                ),
                            }
                            for item in fresh[:2]
                        ],
                    )
                    row["item_failures"] = item_failures
                    if item_failures:
                        row["status"] = "partial"
                except Exception as error:
                    row.update(status="error", error_type=type(error).__name__)
            rows.append(row)

    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "online": online,
        "ai_calls": 0,
        "sources": rows,
        "active_count": sum(row["status"] != "planned" for row in rows),
        "working_count": sum(row["status"] in {"ok", "empty", "partial"} for row in rows),
        "partial_count": sum(row["status"] == "partial" for row in rows),
        "error_count": sum(row["status"] == "error" for row in rows),
    }


def source_health_exit_code(
    result: dict[str, Any],
    *,
    fail_on_error: bool = False,
    fail_on_partial: bool = False,
    require_active: bool = False,
) -> int:
    """Return a CI-friendly exit code without changing audit result semantics."""
    active_count = result.get("active_count", 0)
    error_count = result.get("error_count", 0)
    partial_count = result.get("partial_count", 0)

    if require_active and (not isinstance(active_count, int) or active_count <= 0):
        return 1
    if fail_on_error and (not isinstance(error_count, int) or error_count > 0):
        return 1
    if fail_on_partial and (not isinstance(partial_count, int) or partial_count > 0):
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("cities/belgrade/sources"))
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-error", action="store_true")
    parser.add_argument("--fail-on-partial", action="store_true")
    parser.add_argument("--require-active", action="store_true")
    args = parser.parse_args()

    result = asyncio.run(
        audit_sources(
            args.sources,
            online=args.online,
            source_ids=tuple(args.source_id),
        )
    )
    if args.output:
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False))

    exit_code = source_health_exit_code(
        result,
        fail_on_error=args.fail_on_error,
        fail_on_partial=args.fail_on_partial,
        require_active=args.require_active,
    )
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
