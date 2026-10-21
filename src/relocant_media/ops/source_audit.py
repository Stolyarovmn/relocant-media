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
from relocant_media.sources.registry import SourceConfig, load_source_registry

_TRANSPORT_ATTEMPTS = 3
_TRANSPORT_RETRY_DELAYS = (1.0, 3.0)


async def _collect_with_transport_retry(
    source: SourceConfig,
    client: httpx.AsyncClient,
) -> tuple[list[Any], list[dict[str, str]], int]:
    """Retry bounded transport failures; extraction/data errors remain immediate."""
    for attempt in range(1, _TRANSPORT_ATTEMPTS + 1):
        collector = build_collector(source, client)
        try:
            items = await collector.collect()
            failures = getattr(collector, "item_failures", [])
            return items, failures, attempt
        except httpx.TransportError:
            if attempt >= _TRANSPORT_ATTEMPTS:
                raise
            await asyncio.sleep(_TRANSPORT_RETRY_DELAYS[attempt - 1])
    raise AssertionError("unreachable")


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
                    items, item_failures, attempts = await _collect_with_transport_retry(
                        source,
                        client,
                    )
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
                        attempts=attempts,
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


def render_source_health_markdown(result: dict[str, Any]) -> str:
    """Render a compact Actions summary without relying on artifact storage."""
    lines = [
        "## Source health",
        "",
        f"- checked_at: `{result.get('checked_at', 'unknown')}`",
        f"- active: **{result.get('active_count', 0)}**",
        f"- working: **{result.get('working_count', 0)}**",
        f"- partial: **{result.get('partial_count', 0)}**",
        f"- errors: **{result.get('error_count', 0)}**",
        "",
        "| Source | Status | Fresh | Details |",
        "|---|---|---:|---|",
    ]
    for row in result.get("sources", []):
        if row.get("status") == "planned":
            continue
        details = ""
        if row.get("status") == "error":
            details = str(row.get("error_type", "error"))
        elif row.get("status") == "partial":
            details = f"item_failures={len(row.get('item_failures', []))}"
        elif row.get("attempts", 1) > 1:
            details = f"attempts={row['attempts']}"
        lines.append(
            "| {id} | {status} | {fresh} | {details} |".format(
                id=row.get("id", "unknown"),
                status=row.get("status", "unknown"),
                fresh=row.get("fresh_items", "-"),
                details=details,
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("cities/belgrade/sources"))
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary", type=Path)
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
    if args.summary:
        args.summary.write_text(render_source_health_markdown(result), encoding="utf-8")
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
